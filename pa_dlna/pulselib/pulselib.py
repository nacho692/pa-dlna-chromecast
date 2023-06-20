"""The ctypes interface to the pulselib library."""

import asyncio
import logging
import ctypes as ct
from ctypes.util import find_library

from .pulseaudio_h import *
from .mainloop import MainLoop, get_ctype, CALLBACK_TYPES, callback_func_ptr
from .prototypes import PROTOTYPES

logger = logging.getLogger('pulslib')

# Map some of the values defined in pulseaudio_h to their name.
ERROR_CODES = dict((eval(var), var) for var in globals() if
                   var.startswith('PA_ERR_') or var == 'PA_OK')
CTX_STATES = dict((eval(state), state) for state in
                  ('PA_CONTEXT_UNCONNECTED', 'PA_CONTEXT_CONNECTING',
                   'PA_CONTEXT_AUTHORIZING', 'PA_CONTEXT_SETTING_NAME',
                   'PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                   'PA_CONTEXT_TERMINATED'))
def event_codes_to_names():
    def build_events_dict(mask):
        for fac in globals():
            if fac.startswith(prefix):
                val = eval(fac)
                if (val & mask) and val != mask:
                    yield val, fac[prefix_len:].lower()

    prefix = 'PA_SUBSCRIPTION_EVENT_'
    prefix_len = len(prefix)
    facilities = {0 : 'sink'}
    facilities.update(build_events_dict(PA_SUBSCRIPTION_EVENT_FACILITY_MASK))
    event_types = {0: 'new'}
    event_types.update(build_events_dict(PA_SUBSCRIPTION_EVENT_TYPE_MASK))
    return facilities, event_types

# Dictionaries mapping pulselib events values to their names.
EVENT_FACILITIES, EVENT_TYPES = event_codes_to_names()

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

async def handle_operation(c_operation, future, errmsg):
    if c_operation is None:
        future.cancel()
        raise PulseOperationError(errmsg)

    try:
        await future
        return True
    except asyncio.CancelledError:
        pa_operation_cancel(c_operation)
        return False
    finally:
        pa_operation_unref(c_operation)

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

class PulseLibError(Exception): pass
class PulseStateError(PulseLibError): pass
class PulseOperationError(PulseLibError): pass

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

class PulseEvent:
    """A pulselib event.

    Use the event_facilities() and event_types() static methods to get the
    values defined by the pulselib library for 'facility' and 'type'. They
    correspond to some of the constants defined in the pulseaudio_h module
    under the pa_subscription_event_type_t Enum.
    """

    def __init__(self, event_type, index):
        fac = event_type & PA_SUBSCRIPTION_EVENT_FACILITY_MASK
        assert fac in EVENT_FACILITIES
        self.facility = EVENT_FACILITIES[fac]

        type = event_type & PA_SUBSCRIPTION_EVENT_TYPE_MASK
        assert type in EVENT_TYPES
        self.type = EVENT_TYPES[type]

        self.index = index

    @staticmethod
    def event_facilities():
        return list(EVENT_FACILITIES.values())

    @staticmethod
    def event_types():
        return list(EVENT_TYPES.values())

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

        This method may be invoked at any time to change the subscription
        masks currently set, even from within the async for loop that iterates
        over the reception of pulselib events.
        """

        def context_success_callback(c_context, success, c_userdata):
            handle_context_success('pa_context_subscribe',
                                   success_notification, success)

        success_notification  = self.loop.create_future()
        c_context_success_callback = callback_func_ptr(
                        'pa_context_success_cb_t', context_success_callback)
        c_operation = pa_context_subscribe(self.c_context, subscription_masks,
                                           c_context_success_callback, None)

        errmsg = f'Cannot subscribe events with {subscription_masks} mask'
        await handle_operation(c_operation, success_notification, errmsg)

    def get_events(self):
        """Return an Asynchronous Iterator of pulselib events.

        The iterator is used to run an async for loop over pulselib events.
        Calling this method while an async for loop is already running will
        terminate this loop.

        The async for loop can be terminated by invoking the close() method of
        the iterator from within the loop or from another task.
        """

        if self.event_iterator is not None:
            self.event_iterator.close()
        self.event_iterator = EventIterator()
        return self.event_iterator

    @run_in_task()
    async def pa_context_load_module(self, name, argument):
        def context_index_callback(c_context, index, c_userdata):
            if not index_notification.done():
                index_notification.set_result(index)

        index_notification  = self.loop.create_future()
        c_context_index_callback = callback_func_ptr('pa_context_index_cb_t',
                                             context_index_callback)
        c_operation = pa_context_load_module(self.c_context, name.encode(),
                        argument.encode(), c_context_index_callback, None)

        errmsg = f"Cannot load '{name}' module with '{argument}' argument"
        result = await handle_operation(c_operation, index_notification,
                                        errmsg)
        if result:
            index = index_notification.result()
            if index == PA_INVALID_INDEX:
                raise PulseOperationError(errmsg)
            return index

    @run_in_task()
    async def pa_context_unload_module(self, index):
        def context_success_callback(c_context, success, c_userdata):
            handle_context_success('pa_context_unload_module',
                                   success_notification, success)

        success_notification  = self.loop.create_future()
        c_context_success_callback = callback_func_ptr(
                        'pa_context_success_cb_t', context_success_callback)
        c_operation = pa_context_unload_module(self.c_context, index,
                                            c_context_success_callback, None)

        # Note that this operation fails (c_operation is None) only if the
        # 'index' argument is PA_INVALID_INDEX !
        # See command_kill() in pulselib instrospect.c.
        errmsg = f'Cannot unload module at {index} index'
        await handle_operation(c_operation, success_notification, errmsg)


    # Private methods.
    @staticmethod
    def _context_state_callback(c_context, c_userdata):
        """Call back that monitors the connection state."""

        state = pa_context_get_state(c_context)
        state = CTX_STATES[state]
        if state in ('PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                     'PA_CONTEXT_TERMINATED'):
            error = pa_context_errno(c_context)
            error = ERROR_CODES[error]
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
            raise PulseStateError(self.state)

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
            pulse_lib.event_iterator.put_nowait(PulseEvent(event_type, index))

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
                logger.info('Disconnecting from pulselib context')
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
                raise PulseStateError(self.state)
        except Exception:
            self._close()
            raise

    async def __aexit__(self, exc_type, exc_value, traceback):
        self._close()
        if exc_type is asyncio.CancelledError:
            if self.state[0] != 'PA_CONTEXT_READY':
                raise PulseStateError(self.state)
