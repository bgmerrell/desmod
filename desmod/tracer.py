from __future__ import print_function
try:
    from contextlib import ExitStack
except ImportError:
    from contextlib2 import ExitStack
import re
import sys
import traceback

import simpy
from vcd import VCDWriter

from . import probe
from .util import partial_format


class Tracer(object):

    name = ''

    def __init__(self, env):
        self.env = env
        self.exit_stack = ExitStack()
        cfg_scope = 'sim.' + self.name + '.'
        self.enabled = env.config.get(cfg_scope + 'enable', False)
        if self.enabled:
            self.open()
            include_pat = env.config.get(cfg_scope + 'include_pat', ['.*'])
            exclude_pat = env.config.get(cfg_scope + 'exclude_pat', [])
            self._include_re = [re.compile(pat) for pat in include_pat]
            self._exclude_re = [re.compile(pat) for pat in exclude_pat]

    def is_scope_enabled(self, scope):
        return (self.enabled and
                any(r.match(scope) for r in self._include_re) and
                not any(r.match(scope) for r in self._exclude_re))

    def open(self):
        raise NotImplementedError()

    def close(self):
        self.exit_stack.close()

    def activate_probe(self, scope, target, **hints):
        raise NotImplementedError()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class LogTracer(Tracer):

    name = 'log'
    default_format = '{level} {ts:.3f} {ts_unit}: {scope}: {message}'

    levels = {
        'ERROR': 1,
        'WARNING': 2,
        'INFO': 3,
        'PROBE': 4,
        'DEBUG': 5,
    }

    def open(self):
        log_filename = self.env.config.get('sim.log.file')
        self.max_level = self.levels[self.env.config.get('sim.log.level',
                                                         'INFO')]
        self.format_str = self.env.config.get('sim.log.format',
                                              self.default_format)
        ts_n, ts_unit = self.env.timescale
        if ts_n == 1:
            self.ts_unit = ts_unit
        else:
            self.ts_unit = '({}{})'.format(ts_n, ts_unit)

        if log_filename:
            self.file = open(log_filename, 'w')
            self.exit_stack.enter_context(self.file)
        else:
            self.file = sys.stderr

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type and self.enabled:
            tb_lines = traceback.format_exception(exc_type, exc_val, exc_tb)
            print(self.format_str.format(level='ERROR',
                                         ts=self.env.now,
                                         ts_unit=self.ts_unit,
                                         scope='Exception',
                                         message=tb_lines[-1]),
                  '\n', *tb_lines, file=self.file)
        self.close()

    def is_scope_enabled(self, scope, level=None):
        return ((level is None or self.levels[level] <= self.max_level) and
                super(LogTracer, self).is_scope_enabled(scope))

    def get_log_function(self, scope, level):
        if self.is_scope_enabled(scope, level):
            format_str = partial_format(self.format_str,
                                        level=level,
                                        ts_unit=self.ts_unit,
                                        scope=scope)

            def log_function(message, *args):
                print(format_str.format(ts=self.env.now, message=message),
                      *args, file=self.file)
        else:
            def log_function(message, *args):
                pass

        return log_function

    def activate_probe(self, scope, target, **hints):
        log_hints = hints.get('log', {})
        level = log_hints.get('level', 'PROBE')
        if not self.is_scope_enabled(scope, level):
            return None
        value_fmt = log_hints.get('value_fmt', '{value}')
        format_str = partial_format(self.format_str,
                                    level=level,
                                    ts_unit=self.ts_unit,
                                    scope=scope)

        def probe_callback(value):
            print(format_str.format(ts=self.env.now,
                                    message=value_fmt.format(value=value)),
                  file=self.file)

        return probe_callback


class VCDTracer(Tracer):

    name = 'vcd'

    def open(self):
        dump_filename = self.env.config['sim.vcd.dump_file']
        self.vcd = VCDWriter(
            self.exit_stack.enter_context(open(dump_filename, 'w')),
            timescale=self.env.timescale,
            check_values=self.env.config.get('sim.vcd.check_values', True))
        self.exit_stack.enter_context(self.vcd)
        if self.env.config.get('sim.gtkw.live'):
            from vcd.gtkw import spawn_gtkwave_interactive
            save_filename = self.env.config['sim.gtkw.file']
            spawn_gtkwave_interactive(dump_filename, save_filename, quiet=True)

    def activate_probe(self, scope, target, **hints):
        assert self.enabled
        vcd_hints = hints.get('vcd', {})
        var_type = vcd_hints.get('var_type')
        if var_type is None:
            if isinstance(target, simpy.Container):
                if isinstance(target.level, float):
                    var_type = 'real'
                else:
                    var_type = 'integer'
            elif isinstance(target, (simpy.Resource, simpy.Store)):
                var_type = 'integer'
            else:
                raise ValueError(
                    'Could not infer VCD var_type for {}'.format(scope))

        kwargs = {k: vcd_hints[k]
                  for k in ['size', 'init', 'ident']
                  if k in vcd_hints}

        if var_type == 'integer':
            register_meth = self.vcd.register_int
        elif var_type == 'real':
            register_meth = self.vcd.register_real
        elif var_type == 'event':
            register_meth = self.vcd.register_event
        else:
            register_meth = self.vcd_register_var
            kwargs['var_type'] = var_type

        if 'init' not in kwargs:
            if isinstance(target, simpy.Container):
                kwargs['init'] = target.level
            elif isinstance(target, simpy.Resource):
                kwargs['init'] = len(target.users) if target.users else 'z'
            elif isinstance(target, simpy.Store):
                kwargs['init'] = len(target.items)

        parent_scope, name = scope.rsplit('.', 1)
        var = register_meth(parent_scope, name, **kwargs)

        def probe_callback(value):
            self.vcd.change(var, self.env.now, value)

        return probe_callback


class TraceManager(object):

    def __init__(self, env):
        self.exit_stack = ExitStack()
        self.log_tracer = self.exit_stack.enter_context(LogTracer(env))
        self.vcd_tracer = self.exit_stack.enter_context(VCDTracer(env))
        self.tracers = [self.log_tracer, self.vcd_tracer]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exit_stack.__exit__(*exc)

    def auto_probe(self, scope, target, **hints):
        callbacks = []
        for tracer in self.tracers:
            if tracer.name in hints and tracer.is_scope_enabled(scope):
                callback = tracer.activate_probe(scope, target, **hints)
                if callback:
                    callbacks.append(callback)
        if callbacks:
            probe.attach(scope, target, callbacks, **hints)
