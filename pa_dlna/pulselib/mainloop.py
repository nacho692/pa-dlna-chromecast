"""An implementation of the pulselib Main Loop based on asyncio."""

import asyncio
import logging
import functools
import ctypes as ct

from .pulseaudio_h import *
from .prototypes import CALLBACKS
from .structures import PA_SINK_INFO, PA_SINK_INPUT_INFO, TIMEVAL

debug = 0
logger = logging.getLogger('pulslib')

@functools.lru_cache
def get_ctype(name):
    map = { 'void':                         None,
            'int':                          ct.c_int,
            'uint32_t':                     ct.c_uint32,
            'char *':                       ct.c_char_p,
            'struct timeval *':             TIMEVAL,
            'pa_sink_info *':               ct.POINTER(PA_SINK_INFO),
            'pa_sink_input_info *':         ct.POINTER(PA_SINK_INPUT_INFO),
           }
    for enum_name in PA_ENUM_LIST:
        map[enum_name] = ct.c_int

    if name in map:
        return map[name]
    elif name.endswith('*'):
        return ct.c_void_p
    else:
        return False

def callback_types():
    """Build a dictionary mapping callbacks to their ctypes type."""

    def callback_type(func_name):
        types = []
        val = CALLBACKS[func_name]
        restype = get_ctype(val[0])     # The return type.
        assert restype is not False
        types.append(restype)

        for arg in val[1]:              # The args types.
            argtype = get_ctype(arg)

            # Not a known data type. So it must be a function pointer to a
            # callback. Find its description in CALLBACKS and call
            # callback_type() recursively.
            if argtype is False:
                assert arg in CALLBACKS
                # First check if it's already in the dictionary.
                argtype = map.get(arg)
                if argtype is None:
                    argtype = callback_type(arg)

            types.append(argtype)

        if debug:
            print(f'{func_name}: {types}')

        return ct.CFUNCTYPE(*types)

    map = dict()
    for name in CALLBACKS:
        map[name] = callback_type(name)
    return map

PULSE_CALLBACK_TYPES = callback_types()

# Main Loop API functions.

class MainLoop:
    """An implementation of the pulselib Main Loop based on asyncio.

    Use the get_instance() static method to instantiate MainLoop or to get
    the current instance.
    """

    _asyncio_loops = dict()             # {asyncio loop: MainLoop instance}

    def __init__(self, loop):
        assert loop not in MainLoop._asyncio_loops
        self.loop = loop
        self._asyncio_loops[loop] = self

    @staticmethod
    def get_instance():
        loop = asyncio.get_running_loop()
        mloop = MainLoop._asyncio_loops.get(loop)
        return mloop if mloop is not None else MainLoop(loop)

    def close(self):
        loops = MainLoop._asyncio_loops
        for loop, mloop in loops.items():
            if mloop is self:
                del loops[loop]
        else:
            assert False, 'Cannot remove the MainLoop instance upon closing'
