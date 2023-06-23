"""The ctypes interface to the pulselib library."""

import asyncio
import logging
import ctypes as ct
from ctypes.util import find_library

from .pulseaudio_h import *
from .mainloop import MainLoop, get_ctype, CALLBACK_TYPES, callback_func_ptr
from .prototypes import PROTOTYPES
from .structures import (PA_SAMPLE_SPEC, PA_CHANNEL_MAP, PA_CVOLUME,
                         PA_SINK_INFO, PA_SINK_INPUT_INFO)

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

def run_in_task(coro):
    """Decorator to wrap a coroutine in a task of AsyncioTasks instance."""

    async def wrapper(*args, **kwargs):
        pulse_lib = PulseLib.get_instance()
        try:
            return await pulse_lib.pulselib_tasks.create_task(
                                                    coro(*args, **kwargs))
        except asyncio.CancelledError:
            logger.error(f'{coro.__qualname__}() has been cancelled')
            raise
    return wrapper

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

    Use the event_facilities() and event_types() static methods to get all the
    values currently defined by the pulselib library for 'facility' and
    'type'. They correspond to some of the variables defined in the
    pulseaudio_h module under the pa_subscription_event_type_t Enum.

    attributes:
        facility:   str - name of the facility, for example 'sink'.
        index:      int - index of the facility.
        type:       str - type of event, normaly 'new', 'change' or 'remove'.
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

class PropList:
    def __init__(self, c_pa_proplist):
        null_ptr = ct.POINTER(ct.c_void_p)()
        null_ptr_ptr = ct.pointer(null_ptr)
        while True:
            key = pa_proplist_iterate(c_pa_proplist, null_ptr_ptr)
            if isinstance(key, bytes):
                val = pa_proplist_gets(c_pa_proplist, key)
                if bool(val):
                    setattr (self, key.decode(), val.decode())
            elif not bool(key):
                break

class PulseStructure:
    def __init__(self, c_struct, c_struct_type):
        # Make a deep copy of the elements of the structure as they are only
        # temporarily available.
        for name, c_type in c_struct_type._fields_:
            c_struct_val = getattr(c_struct, name)
            if not bool(c_struct_val):
                continue
            if c_type is ct.c_char_p:
                setattr(self, name, c_struct_val.decode())
            elif c_type in (ct.c_int, ct.c_uint32, ct.c_uint64, ct.c_uint8):
                setattr(self, name, int(c_struct_val))
            elif c_type is PA_SAMPLE_SPEC:
                setattr(self, name, SampleSpec(c_struct_val))
            elif c_type is PA_CHANNEL_MAP:
                setattr(self, name, ChannelMap(c_struct_val))
            elif c_type is PA_CVOLUME:
                setattr(self, name, CVolume(c_struct_val))
            elif name == 'proplist':
                setattr(self, name, PropList(c_struct_val))

class SampleSpec(PulseStructure):
    def __init__(self, c_struct):
        super().__init__(c_struct, PA_SAMPLE_SPEC)

class ChannelMap(PulseStructure):
    def __init__(self, c_struct):
        super().__init__(c_struct, PA_CHANNEL_MAP)

class ChannelMap(PulseStructure):
    def __init__(self, c_struct):
        super().__init__(c_struct, PA_CHANNEL_MAP)

class CVolume(PulseStructure):
    def __init__(self, c_struct):
        super().__init__(c_struct, PA_CVOLUME)

class Sink(PulseStructure):
    """A pulseaudio sink.

    The attribute names of an instance of this class form a subset of the
    names of the _fields_ of the PA_SINK_INFO structure defined in the
    'structures' module. Among them, 'proplist' is a dictionary of the
    properties of the pulseaudio sink whose value is a string.
    """

    def __init__(self, c_struct):
        super().__init__(c_struct, PA_SINK_INFO)

class SinkInput(PulseStructure):
    """A pulseaudio sink input.

    The attribute names of an instance of this class form a subset of the
    names of the _fields_ of the PA_SINK_INPUT_INFO structure defined in the
    'structures' module. Among them, 'proplist' is a dictionary of the
    properties of the pulseaudio sink input whose value is a string.
    """

    def __init__(self, c_struct):
        super().__init__(c_struct, PA_SINK_INPUT_INFO)

class PulseLib:
    """Interface to the pulselib library."""

    ASYNCIO_LOOPS = dict()              # {asyncio loop: PulseLib instance}

    # Public methods.
    def __init__(self, name):
        """'name' is the name of the application."""

        assert isinstance(name, str)
        self.c_context = pa_context_new(MainLoop.C_MAINLOOP_API,
                                        name.encode())
        # From the ctypes documentation: "NULL pointers have a False
        # boolean value".
        if not bool(self.c_context):
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

    @staticmethod
    def get_instance():
        loop = asyncio.get_running_loop()
        try:
            return PulseLib.ASYNCIO_LOOPS[loop]
        except KeyError:
            raise PulseOperationError('The PulseLib instance is closed')

    @run_in_task
    async def pa_context_subscribe(self, subscription_masks):
        """Enable event notification.

        'subscription_masks' is built by ORing the masks defined by the
        'pa_subscription_mask_t' Enum in the pulseaudio_h module.

        This method may be invoked at any time to change the subscription
        masks currently set, even from within the async for loop that iterates
        over the reception of pulselib events.
        """

        def context_success_callback(c_context, success, c_userdata):
            PulseLib._success_callback('pa_context_subscribe', notification,
                                       success)

        notification  = self.loop.create_future()
        c_context_success_callback = callback_func_ptr(
                        'pa_context_success_cb_t', context_success_callback)
        c_operation = pa_context_subscribe(self.c_context, subscription_masks,
                                           c_context_success_callback, None)

        errmsg = f'Cannot subscribe events with {subscription_masks} mask'
        await PulseLib._handle_operation(c_operation, notification, errmsg)

    def get_events(self):
        """Return an Asynchronous Iterator of pulselib events.

        The iterator is used to run an async for loop over the PulseEvent
        instances. Calling this method while an async for loop is already
        running will terminate this loop.

        The async for loop can be terminated by invoking the close() method of
        the iterator from within the loop or from another task.
        """
        if self.closed:
            raise PulseOperationError('The PulseLib instance is closed')

        if self.event_iterator is not None:
            self.event_iterator.close()
        self.event_iterator = EventIterator()
        return self.event_iterator

    @run_in_task
    async def pa_context_load_module(self, name, argument):
        """Load the pulseaudio module named 'name' and return its index.

        'argument' is a string. Note that space characters within the double
        quoted strings MUST be escaped with '\'.
        For example:
        sink_name="foo" sink_properties=device.description="foo\ description"
        """

        def context_index_callback(c_context, index, c_userdata):
            if not notification.done():
                notification.set_result(index)

        notification  = self.loop.create_future()
        c_context_index_callback = callback_func_ptr('pa_context_index_cb_t',
                                             context_index_callback)
        c_operation = pa_context_load_module(self.c_context, name.encode(),
                        argument.encode(), c_context_index_callback, None)

        errmsg = f"Cannot load '{name}' module with '{argument}' argument"
        result = await PulseLib._handle_operation(c_operation, notification,
                                                  errmsg)
        if result:
            index = notification.result()
            if index == PA_INVALID_INDEX:
                raise PulseOperationError(errmsg)
            return index

    @run_in_task
    async def pa_context_unload_module(self, index):
        """Unload the module whose index is 'index'.

        Note that this operation fails only if the 'index' argument is
        PA_INVALID_INDEX !
        See command_kill() in pulselib instrospect.c.
        """

        def context_success_callback(c_context, success, c_userdata):
            PulseLib._success_callback('pa_context_unload_module',
                                       notification, success)

        notification  = self.loop.create_future()
        c_context_success_callback = callback_func_ptr(
                        'pa_context_success_cb_t', context_success_callback)
        c_operation = pa_context_unload_module(self.c_context, index,
                                            c_context_success_callback, None)

        errmsg = f'Cannot unload module at {index} index'
        await PulseLib._handle_operation(c_operation, notification, errmsg)

    @run_in_task
    async def pa_context_get_sink_info_list(self):
        """Return the list of Sink instances."""

        def sink_info_callback(c_context, c_sink_info, eol, c_userdata):
            PulseLib._list_callback(notification, sink_infos, Sink,
                                    c_sink_info, eol)

        sink_infos = []
        notification  = self.loop.create_future()
        c_sink_info_callback = callback_func_ptr('pa_sink_info_cb_t',
                                                 sink_info_callback)
        c_operation = pa_context_get_sink_info_list(self.c_context,
                                                c_sink_info_callback, None)
        errmsg = 'Error at pa_context_get_sink_info_list()'
        eol  = await PulseLib._handle_operation(c_operation, notification,
                                                errmsg)
        if eol < 0:
            raise PulseOperationError(errmsg)
        return sink_infos

    @run_in_task
    async def pa_context_get_sink_input_info_list(self):
        """Return the list of SinkInput instances."""

        def sink_input_info_callback(c_context, c_sink_input_info, eol,
                                     c_userdata):
            PulseLib._list_callback(notification, sink_input_infos, SinkInput,
                                    c_sink_input_info, eol)

        sink_input_infos = []
        notification  = self.loop.create_future()
        c_sink_input_info_callback = callback_func_ptr(
                        'pa_sink_input_info_cb_t', sink_input_info_callback)
        c_operation = pa_context_get_sink_input_info_list(self.c_context,
                                            c_sink_input_info_callback, None)
        errmsg = 'Error at pa_context_get_sink_input_info_list()'
        eol  = await PulseLib._handle_operation(c_operation, notification,
                                                errmsg)
        if eol < 0:
            raise PulseOperationError(errmsg)
        return sink_input_infos

    @run_in_task
    async def pa_context_get_sink_info_by_name(self, name):
        """Return the Sink instance whose name is 'name'."""

        def sink_info_callback(c_context, c_sink_info, eol, c_userdata):
            PulseLib._list_callback(notification, sink_infos, Sink,
                                    c_sink_info, eol)

        sink_infos = []
        notification  = self.loop.create_future()
        c_sink_info_callback = callback_func_ptr('pa_sink_info_cb_t',
                                                 sink_info_callback)
        c_operation = pa_context_get_sink_info_by_name(self.c_context,
                                    name.encode(), c_sink_info_callback, None)
        errmsg = 'Error at pa_context_get_sink_info_by_name()'
        eol  = await PulseLib._handle_operation(c_operation, notification,
                                                errmsg)
        if eol < 0:
            raise PulseOperationError(errmsg)
        return sink_infos[0]


    # Private methods.
    @staticmethod
    def _success_callback(func_name, future, success):
        # 'success' may be PA_OPERATION_DONE or PA_OPERATION_CANCELLED.
        # PA_OPERATION_CANCELLED occurs "as a result of the context getting
        # disconnected while the operation is pending". We just log this event as
        # the PulseLib instance will be aborted following this disconnection.
        if success == PA_OPERATION_CANCELLED:
            logger.debug(f'Got PA_OPERATION_CANCELLED for {func_name}')
        elif not future.done():
            future.set_result(None)

    @staticmethod
    def _list_callback(notification, instances, cls, c_object, eol):
        # From the ctypes documentation: "NULL pointers have a False
        # boolean value".
        if not bool(c_object):
            if not notification.done():
                notification.set_result(eol)
        else:
            instances.append(cls(c_object.contents))

    @staticmethod
    async def _handle_operation(c_operation, future, errmsg):
        # From the ctypes documentation: "NULL pointers have a False
        # boolean value".
        if not bool(c_operation):
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

    @staticmethod
    def _context_state_callback(c_context, c_userdata):
        """Call back that monitors the connection state."""

        state = pa_context_get_state(c_context)
        state = CTX_STATES[state]
        if state in ('PA_CONTEXT_READY', 'PA_CONTEXT_FAILED',
                     'PA_CONTEXT_TERMINATED'):
            error = pa_context_errno(c_context)
            error = ERROR_CODES[error]
            pulse_lib = PulseLib.get_instance()
            pulse_lib.state = (state, error)
            logger.info(f'PulseLib connection: {pulse_lib.state}')

            state_notification = pulse_lib.state_notification
            if not state_notification.done():
                state_notification.set_result(None)
            elif not pulse_lib.closed and state != 'PA_CONTEXT_READY':
                pulse_lib._abort()
        else:
            logger.debug(f'PulseLib connection: {state}')

    @run_in_task
    async def _pa_context_connect(self):
        """Connect the context to the default server."""

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
        """Call back to handle pulseaudio events."""

        pulse_lib = PulseLib.get_instance()
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
