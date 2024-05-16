"""All the ctypes of the pulse library."""

import sys
import sysconfig
import pprint
import functools
import ctypes as ct
from ctypes.util import find_library

from .pulse_types import pulse_types
from .pulse_enums import pulse_enums
from .pulse_structs import pulse_structs
from .pulse_functions import pulse_functions

# /usr/include/pulse/def.h:
# #define PA_INVALID_INDEX ((uint32_t) -1)
PA_INVALID_INDEX = ct.c_uint32(-1).value

class PulseCTypesError(Exception): pass
class PulseCTypesLibError(PulseCTypesError): pass
class PulseCTypesNameError(PulseCTypesError): pass
class PulseCTypesSignatureError(PulseCTypesError): pass
class PulseCTypesCallbackError(PulseCTypesError): pass

def _time_t():
    """Return a ctypes type for time_t."""

    # The size of 'time_t' depends on the platform.
    # 'SIZEOF_TIME_T' is computed by configure when building Python.
    sizeof_time_t = sysconfig.get_config_var('SIZEOF_TIME_T')
    types = (ct.c_longlong, ct.c_long, ct.c_int)
    sizes = [ct.sizeof(t) for t in types]
    assert sizeof_time_t in sizes, 'Cannot find a ctypes match for time_t.'
    return types[sizes.index(sizeof_time_t)]

class timeval(ct.Structure):
    _fields_ = [
        ('tv_sec', _time_t()),
        ('tv_usec', ct.c_long),
    ]

class PulseCTypes:

    standard_ctypes = {
        'void':         None,
        'int':          ct.c_int,
        'int64_t':      ct.c_int64,
        'unsigned':     ct.c_uint,
        'unsigned int': ct.c_uint,
        'unsigned long':ct.c_ulong,
        'uint8_t':      ct.c_uint8,
        'uint32_t':     ct.c_uint32,
        'uint64_t':     ct.c_uint64,
        'size_t':       ct.c_size_t,
        'float':        ct.c_float,
        'char *':       ct.c_char_p,
        'void *':       ct.c_void_p,
    }

    def __init__(self):
        self.known_ctypes = {}
        self.struct_ctypes = {}
        self.cb_types_params = None

        path = find_library('pulse')
        if path is None:
            raise PulseCTypesLibError('Cannot find the pulse library')
        self.clib = ct.CDLL(path)

        self.update_known_ctypes()
        self.update_struct_ctypes()

    def update_known_ctypes(self):
        for item in pulse_types:
            types = pulse_types[item].split()
            length = len(types)
            if length == 1:
                self.known_ctypes[item] = self.get_ctype(types[0])
                continue
            elif length == 2:
                if types[0] == 'enum':
                    self.known_ctypes[item] = ct.c_int
                    continue
                elif types[0] == 'struct' and item == types[1]:
                    continue
            raise PulseCTypesError(f'Unknown type: {item}: {types}')

    def _ctype_struct_class(self, types):
        # ctype_struct_class() helper.
        if types[0] in pulse_structs or types[0] == 'timeval':
            # Recursive call.
            struct_nested = self.ctype_struct_class(types[0])
            if types[-1] == '*':
                ctype = ct.POINTER(struct_nested)
            else:
                ctype = struct_nested
        else:
            ctype = self.get_ctype(' '.join(types))
        return ctype

    def ctype_struct_class(self, struct):
        """Build a ctypes Structure class."""

        if struct in self.struct_ctypes:
            return self.struct_ctypes[struct]

        _fields_ = []
        for member in pulse_structs[struct]:
            member_name = member[0]
            member_type = member[1]
            types = member_type.split()
            if types[0] == 'struct':
                types = types[1:]

            length = len(types)
            assert length <= 3
            if length == 3:
                if ''.join(types).endswith('**'):
                    ctype = ct.c_void_p
                    _fields_.append((member_name, ctype))
                    continue
                elif types[1] == '*':
                    try:
                        base_ctype = self._ctype_struct_class(types[:1])
                        # An array of ctypes.
                        ctype = (base_ctype * int(types[-1]))
                        _fields_.append((member_name, ctype))
                        continue
                    except ValueError:
                        pass

            assert length <= 2
            if member_type.endswith('**'):
                ctype = ct.c_void_p
            else:
                ctype = self._ctype_struct_class(types)
            _fields_.append((member_name, ctype))

        # Create the Structure subclass.
        struct_class = type(struct, (ct.Structure, ),
                                            {'_fields_': tuple(_fields_)})
        return struct_class

    def update_struct_ctypes(self):
        self.struct_ctypes['timeval'] = timeval
        self.struct_ctypes['timeval *'] = ct.POINTER(timeval)

        for struct in pulse_structs:
            struct_class = self.ctype_struct_class(struct)
            if len(struct_class._fields_) != 0:
                self.struct_ctypes[struct] = struct_class
                self.struct_ctypes[struct + ' *'] = ct.POINTER(struct_class)

    def get_ctype(self, str_type):
        if str_type.startswith('struct '):
            str_type = str_type[7:]
        if str_type in self.standard_ctypes:
            return self.standard_ctypes[str_type]
        elif str_type in self.known_ctypes:
            return self.known_ctypes[str_type]
        elif str_type in self.struct_ctypes:
            return self.struct_ctypes[str_type]
        elif str_type.endswith('*'):
            return ct.c_void_p
        else:
            raise PulseCTypesError(f'Cannot convert to ctypes: {str_type}')

    @functools.lru_cache
    def get_callback(self, callback_name):
        try:
            val = pulse_functions['callbacks'][callback_name]
        except KeyError:
            raise PulseCTypesCallbackError(
                                f"'{callback_name}' not a known callback")

        types = []
        restype = self.get_ctype(val[0])    # The return type.
        types.append(restype)

        for arg in val[1]:                  # The args types.
            try:
                argtype = self.get_ctype(arg)
            except PulseCTypesError:
                # Not a known data type. So it must be a function pointer
                # to a callback. Call get_callback() recursively.
                assert arg in pulse_functions['callbacks'], (
                                    f'{callback_name}: {val} - Error: {arg}')
                argtype = self.get_callback(arg)
            types.append(argtype)

        if self.cb_types_params is not None:
            self.cb_types_params[callback_name] = types

        return ct.CFUNCTYPE(*types)

    @functools.lru_cache
    def get_prototype(self, func_name):
        """Set the restype and argtypes of a 'clib' function name."""

        # Ctypes does not allow None as a NULL callback function pointer.
        # Overriding _CFuncPtr.from_param() allows it. This is a hack as
        # _CFuncPtr is private.
        # See https://ctypes-users.narkive.com/wmJNDPu2/optional-callbacks-
        # passing-null-for-function-pointers.
        def from_param(cls, obj):
            if obj is None:
                return None     # Return a NULL pointer.
            return ct._CFuncPtr.from_param(obj)

        try:
            func = getattr(self.clib, func_name)
        except AttributeError:
            raise PulseCTypesNameError(
                f"'{func_name}' is not a function of the pulse library")
        try:
            val = pulse_functions['signatures'][func_name]
        except KeyError:
            raise PulseCTypesSignatureError(
                                f"'{func_name}' not a known signature")
        func.restype = self.get_ctype(val[0])   # The return type.

        argtypes = []
        for arg in val[1]:                      # The args types.
            if arg == 'void':
                break

            # A function signature nested in this signature.
            if isinstance(arg, tuple):
                types = []
                restype = self.get_ctype(arg[0])    # The return type.
                types.append(restype)
                for argument in arg[1]:             # The args types.
                    ctype = self.get_ctype(argument)
                    types.append(ctype)
                argtype = ct.CFUNCTYPE(*types)
                argtype.from_param = classmethod(from_param)
                argtypes.append(argtype)
                continue

            try:
                argtype = self.get_ctype(arg)
            except PulseCTypesError:
                # Not a known data type. So it must be a function pointer to a
                # callback.
                argtype = self.get_callback(arg)
                argtype.from_param = classmethod(from_param)
            argtypes.append(argtype)

        func.argtypes = argtypes

        return func


def python_object(ctypes_object, cls=None):
    obj = ct.cast(ctypes_object, ct.POINTER(ct.py_object)).contents.value
    if cls is not None:
        assert type(obj) is cls
    return obj

def print_types(sections):
    types = PulseCTypes()

    for section in sections:
        if section == 'types':
            pprint.pprint(types.known_ctypes)

        elif section == 'structs':
            # Structures excluding pointer types.
            pprint.pprint(dict((t.__name__, t._fields_) for
                               t in types.struct_ctypes.values() if
                               hasattr(t, '_fields_')))

        elif section == 'callbacks':
            types.cb_types_params = {}
            for callback_name in pulse_functions['callbacks']:
                types.get_callback(callback_name)
            pprint.pprint(types.cb_types_params)

        elif section == 'prototypes':
            for func_name in pulse_functions['signatures']:
                func = types.get_prototype(func_name)
                pprint.pprint(f'{func.__name__}: '
                              f'({func.restype}, {func.argtypes})')
            print('get_callback: ', types.get_callback.cache_info())

        else:
            print(f"Error: '{section}' is not a valid section name")

def main():
    print_types(sys.argv[1:])

if __name__ == '__main__':
    main()
