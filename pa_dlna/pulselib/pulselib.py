"""The ctypes interface to the pulselib library."""

import asyncio
import logging
import ctypes as ct
from ctypes.util import find_library

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG,
                        format='%(name)-7s %(levelname)-7s %(message)s')

from .pulseaudio_h import *
from .mainloop import MainLoop, get_ctype, CALLBACK_TYPES, callback_func_ptr
from .prototypes import PROTOTYPES

logger = logging.getLogger('pulslib')

# Map some of the values defined in pulseaudio_h to their name.
_ERROR_CODES = dict((eval(var), var) for var in globals() if
                    var.startswith('PA_ERR_') or var == 'PA_OK')
_CTX_STATES = dict((eval(state), state) for state in
                   ('PA_CONTEXT_UNCONNECTED', 'PA_CONTEXT_CONNECTING',
                    'PA_CONTEXT_AUTHORIZING', 'PA_CONTEXT_SETTING_NAME',
                    'PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                    'PA_CONTEXT_TERMINATED'))

def run_in_task():
    """Decorator to wrap a coroutine in a task of AsyncioTasks instance."""

    def decorator(coro):
        async def wrapper(*args, **kwargs):
            loop = asyncio.get_running_loop()
            pulse_lib = PulseLib.ASYNCIO_LOOPS[loop]
            try:
                return await pulse_lib.pulselib_tasks.create_task(
                                                        coro(*args, **kwargs))
            except asyncio.CancelledError:
                logger.error(f'{coro.__qualname__}() has been cancelled')
                raise
        return wrapper
    return decorator

def handle_context_success(func_name, future, success):
    # 'success' may be PA_OPERATION_DONE or PA_OPERATION_CANCELLED.
    # PA_OPERATION_CANCELLED occurs "as a result of the context getting
    # disconnected while the operation is pending". We just log this event as
    # the PulseLib instance will be aborted following this disconnection.
    if success == PA_OPERATION_CANCELLED:
        logger.debug(f'Got PA_OPERATION_CANCELLED for {func_name}')
    elif not future.done():
        future.set_result(None)

def setup_prototype(func_name, pulselib):
    """Set the restype and argtypes of a pulselib function name."""

    # Ctypes does not allow None as a NULL callback function pointer.
    # Overriding _CFuncPtr.from_param() allows it. This is a hack as _CFuncPtr
    # is private.
    # See https://ctypes-users.narkive.com/wmJNDPu2/optional-callbacks-
    # passing-null-for-function-pointers.
    def from_param(cls, obj):
        if obj is None:
            return None     # Return a NULL pointer.
        return ct._CFuncPtr.from_param(obj)

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
            argtype.from_param = classmethod(from_param)
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
class EventIterator:
    def __init__(self):
        self.event_queue = asyncio.Queue()
        self.closed = False

    def put_nowait(self, obj):
        if not self.closed:
            self.event_queue.put_nowait(obj)

    def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            return await self.event_queue.get()
        except asyncio.CancelledError:
            self.close()
            raise StopAsyncIteration

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

    def __init__(self, name):
        self.c_context = pa_context_new(MainLoop.C_MAINLOOP_API,
                                        name.encode())
        if self.c_context is None:
            raise RuntimeError('Cannot get context from pulselib library')

        self.closed = False
        self.state = ('PA_CONTEXT_UNCONNECTED', 'PA_OK')
        self.loop = asyncio.get_running_loop()
        self.main_task = asyncio.current_task(self.loop)
        self.pulselib_tasks = AsyncioTasks()
        self.state_notification = self.loop.create_future()
        self.event_iterator = None
        self.ASYNCIO_LOOPS[self.loop] = self

        # Keep a reference to prevent garbage collection.
        self.c_context_state_callback = callback_func_ptr(
            'pa_context_notify_cb_t', PulseLib._context_state_callback)
        self.c_context_subscribe_callback = callback_func_ptr(
            'pa_context_subscribe_cb_t', PulseLib._context_subscribe_callback)


    # Public methods.
    @run_in_task()
    async def pa_context_subscribe(self, subscription_masks):
        """Enable event notification.

        'subscription_masks' is built by ORing the masks of the
        'pa_subscription_mask_t' Enum defined in the pulseaudio_h module.
        """

        def context_success_callback(c_context, success, c_userdata):
            handle_context_success('pa_context_subscribe',
                                   success_notification, success)

        success_notification  = self.loop.create_future()
        c_context_success_callback = callback_func_ptr(
                        'pa_context_success_cb_t', context_success_callback)
        c_operation = pa_context_subscribe(self.c_context, subscription_masks,
                                           c_context_success_callback, None)
        try:
            await success_notification
        except asyncio.CancelledError:
            pa_operation_cancel(c_operation)
        finally:
            pa_operation_unref(c_operation)

    def get_events(self):
        """Return an Asynchronous Iterator of pulselib events.

        The iterator loop can be terminated by invoking the close() method of
        the iterator.
        """

        if self.event_iterator is not None:
            self.event_iterator.close()
        self.event_iterator = EventIterator()
        return self.event_iterator


    # Private methods.
    @staticmethod
    def _context_state_callback(c_context, c_userdata):
        """Call back that monitors the connection state."""

        state = pa_context_get_state(c_context)
        state = _CTX_STATES[state]
        if state in ('PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                     'PA_CONTEXT_TERMINATED'):
            error = pa_context_errno(c_context)
            error = _ERROR_CODES[error]
            loop = asyncio.get_running_loop()
            pulse_lib = PulseLib.ASYNCIO_LOOPS[loop]
            pulse_lib.state = (state, error)
            logger.info(f'PulseLib connection: {pulse_lib.state}')

            state_notification = pulse_lib.state_notification
            if not state_notification.done():
                state_notification.set_result(None)
            elif not pulse_lib.closed and state != 'PA_CONTEXT_READY':
                pulse_lib._abort()
        else:
            logger.debug(f'PulseLib connection: {state}')

    @run_in_task()
    async def _pa_context_connect(self):
        pa_context_set_state_callback(self.c_context,
                                      self.c_context_state_callback, None)
        rc = pa_context_connect(self.c_context, None, PA_CONTEXT_NOAUTOSPAWN,
                                None)
        logger.debug(f'pa_context_connect return code: {rc}')
        await self.state_notification

        if self.state[0] != 'PA_CONTEXT_READY':
            raise PulseconnectionError(self.state)

        version = pa_context_get_protocol_version(self.c_context)
        server_version = pa_context_get_server_protocol_version(
            self.c_context)
        server_name = pa_context_get_server(self.c_context)
        logger.info(f'Connected to pulseaudio at {server_name.decode()}\n' +
                    16 * ' ' +
                    f'library/server versions: {version}/{server_version}')

    @staticmethod
    def _context_subscribe_callback(c_context, event_type, index, c_userdata):
        loop = asyncio.get_running_loop()
        pulse_lib = PulseLib.ASYNCIO_LOOPS[loop]
        if pulse_lib.event_iterator is not None:
            pulse_lib.event_iterator.put_nowait((event_type, index))

    def _abort(self):
        self.main_task.cancel()
        for task in self.pulselib_tasks:
            task.cancel()
        if self.event_iterator is not None:
            self.event_iterator.close()

    def _close(self):
        if self.closed:
            return
        self.closed = True

        logger.debug('Closing the PulseLib instance')
        try:
            pa_context_set_state_callback(self.c_context, None, None)
            pa_context_set_subscribe_callback(self.c_context, None, None)

            if self.state[0] == 'PA_CONTEXT_READY':
                pa_context_disconnect(self.c_context)
        finally:
            pa_context_unref(self.c_context)

            for loop, pulse_lib in list(self.ASYNCIO_LOOPS.items()):
                if pulse_lib is self:
                    del self.ASYNCIO_LOOPS[loop]
                    break
            else:
                assert False, 'Cannot remove PulseLib instance upon closing'

            MainLoop.close()

    async def __aenter__(self):
        try:
            # Set up the two callbacks that live until this instance is
            # closed.
            await self._pa_context_connect()
            pa_context_set_subscribe_callback(self.c_context,
                                    self.c_context_subscribe_callback, None)
            return self
        except asyncio.CancelledError:
            self._close()
            if self.state[0] != 'PA_CONTEXT_READY':
                raise PulseconnectionError(self.state)
        except Exception:
            self._close()
            raise

    async def __aexit__(self, exc_type, exc_value, traceback):
        self._close()
        if exc_type is asyncio.CancelledError:
            if self.state[0] != 'PA_CONTEXT_READY':
                raise PulseconnectionError(self.state)

async def main():
    try:
        async with PulseLib('pa-dlna') as pulse_lib:
            logger.debug(f'main: connected')

            await pulse_lib.pa_context_subscribe(PA_SUBSCRIPTION_MASK_ALL)
            async for event in pulse_lib.get_events():
                event_type, index = event
                facility = event_type & PA_SUBSCRIPTION_EVENT_FACILITY_MASK
                type = event_type & PA_SUBSCRIPTION_EVENT_TYPE_MASK
                logger.debug(f'index {index}: {hex(facility)} - {hex(type)}')

    except PulseconnectionError as e:
        logger.error(f'{e!r}')
    logger.info('FIN')

if __name__ == '__main__':
    asyncio.run(main())
