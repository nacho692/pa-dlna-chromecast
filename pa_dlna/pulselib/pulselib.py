"""The ctypes interface to the pulselib library."""

import sys
import asyncio
import logging
import ctypes as ct
from ctypes.util import find_library

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG,
                        format='%(name)-7s %(levelname)-7s %(message)s')

from .pulseaudio_h import *
from .mainloop import MainLoop, get_ctype, CALLBACK_TYPES, callback_function
from .prototypes import PROTOTYPES

logger = logging.getLogger('pulslib')

ERROR_CODES = dict((eval(var), var) for var in globals() if
                   var.startswith('PA_ERR_') or var == 'PA_OK')

def run_in_task():
    """A decorator to wrap a coroutine in a task of AsyncioTasks."""

    def decorator(coro):
        async def wrapper(*args, **kwargs):
            loop = asyncio.get_running_loop()
            pulse_ctl = PulseLib.ASYNCIO_LOOPS[loop]
            try:
                return await pulse_ctl.pulselib_tasks.create_task(
                                                        coro(*args, **kwargs))
            except asyncio.CancelledError:
                logger.error(f'{coro.__qualname__}() has been cancelled')
                raise
        return wrapper
    return decorator

def setup_prototype(func_name, pulselib):
    """Set the restype and argtypes of a pulselib function name."""

    func = getattr(pulselib, func_name)
    val = PROTOTYPES[func_name]
    func.restype = get_ctype(val[0])        # The return type.

    argtypes = []
    for arg in val[1]:                      # The args types.
        try:
            argtype = get_ctype(arg)
        except KeyError:
            # Not a known data type. So it must be a function pointer to a
            # callback.
            argtype = CALLBACK_TYPES[arg]
        argtypes.append(argtype)
    func.argtypes = argtypes
    return func

def build_pulselib_prototypes(debug=False):
    """Add the ctypes pulselib functions to the current module namespace."""

    path = find_library('pulse')
    if path is None:
        raise RuntimeError('Cannot find the pulselib library')
    pulselib = ct.CDLL(path)
    _globals = globals()
    for func_name in PROTOTYPES:
        func = setup_prototype(func_name, pulselib)
        _globals[func_name] = func

        if debug:
            logger.debug(f'{func.__name__}: ({func.restype}, {func.argtypes})')

build_pulselib_prototypes()

class PulseconnectionError(Exception): pass
class AsyncioTasks:
    def __init__(self):
        self._tasks = set()

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.remove(t))
        return task

    def __iter__(self):
        for t in self._tasks:
            yield t

class PulseLib:
    """Interface to the pulselib library."""

    ASYNCIO_LOOPS = dict()              # {asyncio loop: PulseLib instance}
    CTX_STATES = dict((eval(state), state) for state in
                      ('PA_CONTEXT_UNCONNECTED', 'PA_CONTEXT_CONNECTING',
                       'PA_CONTEXT_AUTHORIZING', 'PA_CONTEXT_SETTING_NAME',
                       'PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                       'PA_CONTEXT_TERMINATED'))

    def __init__(self, name):
        self.c_context = pa_context_new(MainLoop.C_MAINLOOP_API,
                                        name.encode())
        if self.c_context is None:
            raise RuntimeError('Cannot get context from pulselib library')

        self.closed = False
        self.state = 'PA_CONTEXT_UNCONNECTED'
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task(self.loop)
        self.pulselib_tasks = AsyncioTasks()
        # Keep a reference to prevent garbage collection.
        self.c_context_state_callback = callback_function(
                'pa_context_notify_cb_t', PulseLib._context_state_callback)
        self.ASYNCIO_LOOPS[self.loop] = self

    @staticmethod
    def _context_state_callback(c_context, c_userdata):
        """Call back that monitors the connection state."""

        state = pa_context_get_state(c_context)
        state = PulseLib.CTX_STATES[state]
        logger.info(f'PulseLib connection: {state}')
        if state in ('PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                     'PA_CONTEXT_TERMINATED'):
            error = pa_context_errno(c_context)
            loop = asyncio.get_running_loop()
            pulse_ctl = PulseLib.ASYNCIO_LOOPS[loop]
            state_notification = pulse_ctl.state_notification
            if not state_notification.done():
                state_notification.set_result((state, error))
            elif (not pulse_ctl.closed and
                    state_notification != 'PA_CONTEXT_READY'):
                if sys.version_info[:2] >= (3, 9):
                    msg = f'{state}: {ERROR_CODES[error]}'
                    pulse_ctl.task.cancel(msg)
                    for task in pulse_ctl.pulselib_tasks:
                        task.cancel(msg)
                else:
                    pulse_ctl.task.cancel()
                    for task in pulse_ctl.pulselib_tasks:
                        task.cancel()

    @run_in_task()
    async def _pa_context_connect(self):
        self.state_notification = self.loop.create_future()
        pa_context_set_state_callback(self.c_context,
                                      self.c_context_state_callback, None)
        rc = pa_context_connect(self.c_context, None, PA_CONTEXT_NOAUTOSPAWN,
                                None)
        logger.debug(f'pa_context_connect return code: {rc}')
        state = await self.state_notification
        self.state = state[0]
        if self.state != 'PA_CONTEXT_READY':
            raise PulseconnectionError(f'{state[0]}: {ERROR_CODES[state[1]]}')

    def _close(self):
        if self.closed:
            return
        self.closed = True

        logger.info('Closing the PulseLib instance')
        if self.state == 'PA_CONTEXT_READY':
            pa_context_disconnect(self.c_context)
        pa_context_unref(self.c_context)

        for loop, pulse_ctl in list(self.ASYNCIO_LOOPS.items()):
            if pulse_ctl is self:
                del self.ASYNCIO_LOOPS[loop]
                break
        else:
            assert False, 'Cannot remove PulseLib instance upon closing'
        MainLoop.close()

    async def __aenter__(self):
        try:
            await self._pa_context_connect()
            return self
        except asyncio.CancelledError as e:
            self._close()
            msg = e.args[0] if e.args else ''
            raise PulseconnectionError(msg)
        except Exception:
            self._close()
            raise

    async def __aexit__(self, exc_type, exc_value, traceback):
        self._close()
        if exc_type is asyncio.CancelledError:
            msg = exc_value.args[0] if exc_value.args else ''
            raise PulseconnectionError(msg)

async def main():
    try:
        async with PulseLib('pa-dlna') as pulse_ctl:
            logger.debug(f'Connected')
    except PulseconnectionError as e:
        logger.error(f'{e!r}')
    logger.debug('FIN')

if __name__ == '__main__':
    asyncio.run(main())
