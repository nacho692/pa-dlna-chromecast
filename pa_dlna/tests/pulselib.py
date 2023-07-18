import sys
import re
import importlib
import contextlib
import asyncio
import collections.abc
from unittest import mock
from enum import Enum

from . import skip_loop_iterations

SKIP_LOOP_ITERATIONS = 30

@contextlib.contextmanager
def use_pulsectl_stubs(modules):
    """Patch the 'modules' recursively with pulsectl stubs.

    The first module in 'modules' is patched first.
    """

    def recurse_import(modules):
        if len(modules):
            module = modules.pop(0)
            with mock.patch.dict('sys.modules',
                                 {module: importlib.import_module(module)}):
                return recurse_import(modules) + [sys.modules[module]]
        else:
            return []

    for module in modules:
        if module in sys.modules:
            del sys.modules[module]
    importlib.invalidate_caches()

    with mock.patch.dict('sys.modules',
                         {'pulsectl': sys.modules[__name__],
                          'pulsectl_asyncio': sys.modules[__name__]}):
        yield tuple(reversed(recurse_import(modules.copy())))

    for module in modules:
        assert module not in sys.modules

# Stubs of pulsectl's module variables.
class PulseEventTypeEnum(Enum):
    new = ('new', )
    change = ('change', )
    remove = ('remove',)

    def __new__(cls, evt_type):
        obj = object.__new__(cls)
        obj._value = evt_type   # pulsectl uses '_value' and enum '_value_'.
        return obj

class Event:
    def __init__(self, event, proplist=None):
        assert event in PulseEventTypeEnum
        self.t = event      # PulseEventTypeEnum event.
        self.proplist = proplist
        self.index = None

PulseEventMaskEnum = Enum('PulseEventMaskEnum', 'sink sink_input')
class PulseDisconnected(Exception): pass

class SinkInput:
    def __init__(self, name, events):
        assert isinstance(events, collections.abc.Sequence)
        self.name = name
        self.events = events
        self.sink = None
        self.index = None

    def get_event(self):
        if len(self.events):
            event = self.events.pop(0)
            self.proplist = event.proplist
            return event

    def __str__(self):
        return self.name

class Sink:
    index = 0

    def __init__(self, name, owner_module=None):
        self.name = name
        self.owner_module = owner_module
        self.sink_input = None

        self.index = Sink.index
        Sink.index += 1

    def __str__(self):
        return self.name

class PulseAsync():
    """PulseAsync stub."""

    sink_inputs = None
    sink_input_index = 0
    do_raise_once = False

    def __init__(self, name):
        assert self.sink_inputs is not None, ('missing call to'
                                              ' PulseAsync.add_sink_inputs()')

        self.raise_once()
        Sink.index = 0
        Event.index = 0
        self.module_index = 0
        default_sink = Sink('auto-null')    # The pulseaudio default sink.
        self.sinks = [default_sink]

    @classmethod
    def add_sink_inputs(cls, sink_inputs):
        """Extend the list of sink_inputs.

        This class method MUST be called BEFORE the instantiation of
        PulseAsync.
        The first sink_input in the list (if any) is associated with the sink
        loaded by the following call to PulseAsync.module_load().
        """

        cls.sink_inputs = sink_inputs
        for sink_input in sink_inputs:
            index = cls.sink_input_index
            sink_input.index = index
            for event in sink_input.events:
                event.index = index
            cls.sink_input_index += 1

    async def module_load(self, module, args):
        assert module == 'module-null-sink'
        args = dict(re.findall(r"(?P<key>\w+)=\"(?P<value>[^\"]*)\"", args))
        sink_name = args['sink_name'].strip("\"")
        for sink in self.sinks:
            if sink.name == sink_name:
                sink_name = sink_name + '.1'

        index = self.module_index
        sink = Sink(sink_name, owner_module=index)

        # Link this sink to the first sink_input.
        if len(PulseAsync.sink_inputs):
            PulseAsync.sink_inputs[0].sink = sink.index

        self.sinks.append(sink)
        self.module_index += 1
        return index

    async def module_unload(self, index):
        for i, sink in enumerate(list(self.sinks)):
            if sink.owner_module == index:
                self.sinks.pop(i)
                break

    async def sink_list(self):
        return list(sink for sink in self.sinks)

    async def sink_input_list(self):
        return list(sink_input for sink_input in PulseAsync.sink_inputs)

    async def get_sink_by_name(self, name):
        for sink in self.sinks:
            if sink.name == name:
                return sink

    async def subscribe_events(self, mask):
        assert mask == PulseEventMaskEnum.sink_input
        while True:
            has_event = False
            for sink_input in self.sink_inputs:
                event = sink_input.get_event()
                if event is not None:
                    has_event = True
                    yield event
                    # Allow the processing of the event.
                    await skip_loop_iterations(SKIP_LOOP_ITERATIONS)
            if not has_event:
                # The sink_inputs don't have any more events.
                break

    def raise_once(self):
        if self.do_raise_once:
            PulseAsync.do_raise_once = False
            e = Exception()
            cause = Exception('pulse errno 6')
            e.__cause__ = cause
            raise e

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        PulseAsync.sink_inputs = None
        PulseAsync.sink_input_index = 0
        PulseAsync.do_raise_once = False
