"""An implementation of the libpulse Main Loop based on asyncio."""

import sys
import asyncio
import logging
import time
import gc
import ctypes as ct

from .libpulse_ctypes import PulseCTypes, python_object, PulseCTypesLibError
from .pulse_enums import pulse_enums

logger = logging.getLogger('libpuls')

try:
    pulse_ctypes = PulseCTypes()
except PulseCTypesLibError as e:
    sys.exit(f'{e!r}')

pa_io_event_flags = pulse_enums['pa_io_event_flags']

def callback_func_ptr(name, python_function):
    callback = pulse_ctypes.get_callback(name)
    return callback(python_function)

def build_mainloop_api():
    """Build an instance of the libpulse Main Loop API."""

    api = {'io_new': IoEvent.io_new,
           'io_enable': IoEvent.io_enable,
           'io_free': IoEvent.io_free,
           'io_set_destroy': PulseEvent.set_destroy,
           'time_new': TimeEvent.time_new,
           'time_restart': TimeEvent.time_restart,
           'time_free': TimeEvent.time_free,
           'time_set_destroy': PulseEvent.set_destroy,
           'defer_new': DeferEvent.defer_new,
           'defer_enable': DeferEvent.defer_enable,
           'defer_free': DeferEvent.defer_free,
           'defer_set_destroy': PulseEvent.set_destroy,
           'quit': quit
           }

    class Mainloop_api(ct.Structure):
        _fields_ = [('userdata', ct.c_void_p)]
        for name in api:
            _fields_.append((name, pulse_ctypes.get_callback(name)))

    # Instantiate Mainloop_api.
    args = [callback_func_ptr(name, api[name]) for name in api]
    return Mainloop_api(None, *args)

# Main Loop API functions.
# There is only one asyncio loop and therefore only one MainLoop instance per
# thread. The MainLoop instance referenced by any callback of the API is
# obtained by calling MainLoop.get_instance().
class PulseEvent:
    DEBUG = False
    HASHES = []

    def __init__(self, c_callback, c_userdata):
        self.mainloop = MainLoop.get_instance()
        self.c_callback = c_callback
        self.c_userdata = c_userdata
        self.c_destroy_cb = None

        self.c_self = ct.cast(ct.pointer(ct.py_object(self)), ct.c_void_p)
        PulseEvent.HASHES.append(hash(self))
        self.debug(f'__init__ {self.__class__.__name__}')

    def debug(self, msg):
        if PulseEvent.DEBUG:
            index = PulseEvent.HASHES.index(hash(self)) + 1
            logger.debug(f'{index}: {msg}')

    def destroy(self):
        self.debug(f'destroy-0 {self.__class__.__name__}')
        if self.c_destroy_cb:
            self.debug(f'destroy-1 {self.__class__.__name__}')
            self.c_destroy_cb(self.mainloop.C_MAINLOOP_API, self.c_self,
                              self.c_userdata)

    def __del__(self):
        try:
            index = PulseEvent.HASHES.index(hash(self))
        except ValueError:
            return
        PulseEvent.HASHES.pop(index)

    @staticmethod
    def set_destroy(c_event, c_callback):
        event = python_object(c_event)
        event.debug(f'set_destroy {event.__class__.__name__}')
        event.c_destroy_cb = c_callback

    @classmethod
    def cleanup(cls, mainloop):
        for event in cls.EVENTS:
            event.debug(f'cleanup {cls.__name__}')
            if event.mainloop is mainloop:
                event.destroy()

class IoEvent(PulseEvent):
    EVENTS = set()

    def __init__(self, fd, c_callback, c_userdata):
        super().__init__(c_callback, c_userdata)
        self.fd = fd
        self.flags = pa_io_event_flags['PA_IO_EVENT_NULL']

    def read_callback(self):
        self.debug('read_callback IoEvent')
        self.c_callback(self.mainloop.C_MAINLOOP_API, self.c_self, self.fd,
                    pa_io_event_flags['PA_IO_EVENT_INPUT'], self.c_userdata)

    def write_callback(self):
        self.debug('write_callback IoEvent')
        self.c_callback(self.mainloop.C_MAINLOOP_API, self.c_self, self.fd,
                    pa_io_event_flags['PA_IO_EVENT_OUTPUT'], self.c_userdata)

    def enable(self, flags):
        self.debug(f'enable IoEvent: {flags}')
        aio_loop = self.mainloop.aio_loop

        pa_io_event_input = pa_io_event_flags['PA_IO_EVENT_INPUT']
        pa_io_event_output = pa_io_event_flags['PA_IO_EVENT_OUTPUT']
        if flags & pa_io_event_input and not (self.flags & pa_io_event_input):
            aio_loop.add_reader(self.fd, self.read_callback)
        if not (flags & pa_io_event_input) and self.flags & pa_io_event_input:
            aio_loop.remove_reader(self.fd)
        if (flags & pa_io_event_output and
                not (self.flags & pa_io_event_output)):
            aio_loop.add_writer(self.fd, self.write_callback)
        if (not (flags & pa_io_event_output) and
                self.flags & pa_io_event_output):
            aio_loop.remove_writer(self.fd)
        self.flags = flags

    @staticmethod
    def io_new(c_mainloop_api, fd, flags, c_callback, c_userdata):
        event = IoEvent(fd, c_callback, c_userdata)
        event.enable(flags)
        IoEvent.EVENTS.add(event)
        return event.c_self.value

    @staticmethod
    def io_enable(c_io_event, flags):
        event = python_object(c_io_event, cls=IoEvent)
        event.debug(f'io_enable {flags}')
        event.enable(flags)

    @staticmethod
    def io_free(c_io_event):
        event = python_object(c_io_event, cls=IoEvent)
        event.debug('io_free')
        event.enable(pa_io_event_flags['PA_IO_EVENT_NULL'])
        IoEvent.EVENTS.remove(event)

class TimeEvent(PulseEvent):
    EVENTS = set()

    def __init__(self, c_callback, c_userdata):
        super().__init__(c_callback, c_userdata)
        self.timer_handle = None

    def restart(self, c_time):
        if self.timer_handle is not None:
            self.debug('restart TimeEvent - cancel')
            self.timer_handle.cancel()
            self.timer_handle = None
        if c_time is not None:
            timeval = c_time.contents
            delay = timeval.tv_sec + timeval.tv_usec / 10**6 - time.time()
            self.debug(f'restart TimeEvent - delay: {delay}')
            self.timer_handle = self.mainloop.aio_loop.call_later(
                delay, self.c_callback, self.mainloop.C_MAINLOOP_API,
                self.c_self, c_time, self.c_userdata)

    @staticmethod
    def time_new(c_mainloop_api, c_time, c_callback, c_userdata):
        event = TimeEvent(c_callback, c_userdata)
        event.restart(c_time)
        TimeEvent.EVENTS.add(event)
        return event.c_self.value

    @staticmethod
    def time_restart(c_time_event, c_time):
        event = python_object(c_time_event, cls=TimeEvent)
        event.debug('time_restart')
        event.restart(c_time)

    @staticmethod
    def time_free(c_time_event):
        event = python_object(c_time_event, cls=TimeEvent)
        event.debug('time_free')
        event.restart(None)
        TimeEvent.EVENTS.remove(event)

class DeferEvent(PulseEvent):
    EVENTS = set()

    def __init__(self, c_callback, c_userdata):
        super().__init__(c_callback, c_userdata)
        self.handle = None

    def enable(self, enable):
        self.debug(f'enable DeferEvent: {enable}')
        if self.handle is None and enable:
            self.handle = self.mainloop.aio_loop.call_soon(self.callback)
        elif self.handle is not None and not enable:
            self.handle.cancel()
            self.handle = None
        self.enabled = True if enable else False

    def callback(self):
        self.debug('callback DeferEvent')
        self.handle = None
        self.c_callback(self.mainloop.C_MAINLOOP_API, self.c_self,
                        self.c_userdata)
        if self.enabled:
            self.handle = self.mainloop.aio_loop.call_soon(self.callback)

    @staticmethod
    def defer_new(c_mainloop_api, c_callback, c_userdata):
        event = DeferEvent(c_callback, c_userdata)
        event.enable(True)
        DeferEvent.EVENTS.add(event)
        return event.c_self.value

    @staticmethod
    def defer_enable(c_defer_event, enable):
        event = python_object(c_defer_event, cls=DeferEvent)
        event.debug(f'defer_enable {enable}')
        event.enable(enable)

    @staticmethod
    def defer_free(c_defer_event):
        event = python_object(c_defer_event, cls=DeferEvent)
        event.debug('defer_free')
        event.enable(False)
        DeferEvent.EVENTS.remove(event)

def quit(c_mainloop_api, retval):
    logger.debug(f'quit() of the mainloop API called with retval={retval}')

# Keep a reference to the Python Main loop API so that it is not garbage
# collected.
_mainloop_api = build_mainloop_api()

class MainLoop:
    """An implementation of the libpulse Main Loop based on asyncio."""

    ASYNCIO_LOOPS = dict()              # {asyncio loop: MainLoop instance}
    C_MAINLOOP_API = ct.cast(ct.pointer(_mainloop_api), ct.c_void_p)

    def __init__(self, aio_loop):
        assert aio_loop not in MainLoop.ASYNCIO_LOOPS, (
            'There is already a MainLoop instance on this asyncio loop')
        self.aio_loop = aio_loop
        MainLoop.ASYNCIO_LOOPS[aio_loop] = self

    @staticmethod
    def get_instance():
        aio_loop = asyncio.get_running_loop()
        mloop = MainLoop.ASYNCIO_LOOPS.get(aio_loop)
        return mloop if mloop is not None else MainLoop(aio_loop)

    @staticmethod
    def close():
        mloop = MainLoop.get_instance()
        for cls in (IoEvent, TimeEvent, DeferEvent):
            cls.cleanup(mloop)
        gc.collect()

        for aio_loop, loop in list(MainLoop.ASYNCIO_LOOPS.items()):
            if loop is mloop:
                del MainLoop.ASYNCIO_LOOPS[aio_loop]
                break
        else:
            assert False, 'Cannot remove MainLoop instance upon closing'
        logger.info('LibPulse main loop closed')
