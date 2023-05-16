"""Parse the pulseaudio.h header."""

import sys
import os
import re
import shutil
import subprocess
import functools
import pprint

PULSEAUDIO_H = '/usr/include/pulse/pulseaudio.h'
PULSEAUDIO_H_PY = 'pa_dlna/pulselib/pulseaudio_h.py'
PROTOTYPES_PY = 'pa_dlna/pulselib/prototypes.py'

# What we are looking for.
ENUM_TYPEDEFS = { 'pa_context_state', 'pa_operation_state',
                  'pa_error_code', 'pa_subscription_mask',
                  'pa_subscription_event_type', 'pa_sink_state',
                  'pa_io_event_flags',
                }

# The regular expressions.
ENUMS_RE = re.compile(r'typedef\s+enum\s+(pa_\w+)\s*{([^}]+)}\s*pa_\w+\s*;')
CONSTANT_RE = re.compile(r'(\w+)\s*=\s*(0x[0-9A-Fa-f]+|-?\d+)')
PROTOTYPE_RE = re.compile(
                r'\n(\w.*[ *])(pa_\w+\s*?)\(([^)]+)\)\s*(__attribute__)*.*;')
CALLBACK_RE = re.compile(
        r'\n(typedef| )\s*(\w.*[ *])\(\s*\*\s*(\w+\s*)\)\s*\(([^)]+)\)\s*;')
POINTER_RE = re.compile(r'(\w+)\s*(\*+)\s*(\w+\[?]?)?')

def callback_regexps():
    regexps = (
        r'pa_context_notify_cb_t',
        r'pa_sink_info_cb_t',
        r'pa_sink_input_info_cb_t',
        r'pa_context_index_cb_t',
        r'pa_context_subscribe_cb_t',
        r'pa_context_success_cb_t',

        # mainloop API.
        r'io_\w+',
        r'time_\w+',
        r'defer_\w+',
        r'quit',

        # mainloop API callbacks.
        r'pa_defer_event_cb_t',
        r'pa_defer_event_destroy_cb_t',
        r'pa_io_event_cb_t',
        r'pa_io_event_destroy_cb_t',
        r'pa_time_event_cb_t',
        r'pa_time_event_destroy_cb_t',
    )
    return [re.compile(reg) for reg in regexps]

def function_regexps():
    regexps = (
        r'pa_context_new',
        r'pa_context_set_state_callback',
        r'pa_context_errno',
        r'pa_context_\w*ref',
        r'pa_context_\w*connect',
        r'pa_context_\w+module',
        r'pa_context_get_sink_info_by_name',
        r'pa_context_get_sink_\w*info_list',
        r'pa_context_get_state',
        r'pa_context_\w*protocol\w*',
        r'pa_context_\w*subscribe\w*',
        r'pa_operation_\w*ref',
        r'pa_operation_cancel',
    )
    return [re.compile(reg) for reg in regexps]

@functools.lru_cache
def preprocess(pathname):
    proc = subprocess.run(['gcc', '-E', '-P', pathname],
                          capture_output=True, text=True)
    return proc.stdout

def parse_enums(pathname, typedefs=None):
    """Parse enum typedefs in a libpulse header.

    Return the dict: {typedef name: {enum: value}}
    If 'typedefs' if not None, fill the dict with the typedef names in the
    'typedefs' set. Otherwise add all the typedef names that are found.
    """

    typedef_enums = dict()
    for name, enum in re.findall(ENUMS_RE, preprocess(pathname)):
        if typedefs is not None:
            if name not in typedefs:
                continue
            else:
                typedefs.remove(name)

        i = 0
        enums = dict()
        for constant in (x.strip() for x in enum.split(',')):
            if not constant:
                continue
            m = re.match(CONSTANT_RE, constant)
            if m is not None:
                val = m.group(2)
                enums[m.group(1)] = val
                i = eval(val)
            else:
                enums[constant] = i
            i += 1
            typedef_enums[name] = enums

    if typedefs is not None and len(typedefs) != 0:
        print(f'*** Error: could not find the {typedefs} enum(s)'
              f' at {pathname}.', file=sys.stderr)
        sys.exit(1)

    return typedef_enums

def remove_const(txt):
    """Remove the 'constant' C keyword."""

    words = txt.split()
    pruned = []
    for word in words:
        if word == '*const':
            pruned.append('*')
        elif word != 'const':
            pruned.append(word)
    return ' '.join(pruned)

def get_type(arg):
    """Strip arg from the name of its variable if any."""

    # Handle pointer types with any number of consecutive '*' and written as:
    #  'type* arg' 'type * arg' 'type *arg' 'type*arg' -> 'type *'
    #  'type* arg[]' 'type * arg[]' 'type *arg[]' 'type*arg[]' -> 'type **'
    arg = remove_const(arg)
    m = re.match(POINTER_RE, arg)
    if m is not None:
        p = m.group(2)
        varname = m.group(3)
        if varname is not None and varname.endswith('[]'):
            p += '*'
        words = [m.group(1), p]
    else:
        words = [arg.split()[0]]

    return ' '.join(words)

def parse_prototypes(pathname, functions=None):
    """Parse function prototypes in a libpulse header.

    Return the dict: {function name: (restype, [argtypes])}}
    If 'functions' is not None, fill the dict with the function names that
    match one of the regexp in 'functions'. Otherwise add all the functions
    that are found.
    """

    prototypes = dict()
    for res, name, args, _ in re.findall(PROTOTYPE_RE, preprocess(pathname)):
        name = name.strip()
        if functions is not None:
            for func in functions:
                if re.fullmatch(func, name) is not None:
                    break
            else:
                continue

        # Exclude variadic functions, functions with parenthesis in the args
        # list.
        if name in ('pa_mainloop_api_once', 'pa_proplist_setf'):
            continue

        res = get_type(res)
        args = [get_type(x) for x in args.split(',')]
        prototypes[name] = (res, args)

    return prototypes

def parse_callbacks(pathname, functions=None):
    """Parse callbacks in a libpulse header.

    Return the dict: {function name: (restype, [argtypes])}}
    If 'functions' is not None, fill the dict with the callback names that
    match one of the regexp in 'functions'. Otherwise add all the callbacks
    that are found.
    """

    callbacks = dict()
    for _, res, name, args in re.findall(CALLBACK_RE, preprocess(pathname)):
        name = name.strip()
        if functions is not None:
            for func in functions:
                if re.fullmatch(func, name) is not None:
                    break
            else:
                continue

        res = get_type(res)
        args = [get_type(x) for x in args.split(',')]
        callbacks[name] = (res, args)

    return callbacks

def write_header(typedef_enums, fileobj):
    doc = '"""Pulseaudio constants."""\n\n'
    fileobj.write(doc)

    # def.h:#define PA_INVALID_INDEX ((uint32_t) -1)
    fileobj.write('PA_INVALID_INDEX = 0xffffffff\n\n')

    # Write the enums.
    first_one = True
    for typedef, enums in typedef_enums.items():
        if first_one:
            first_one = False
        else:
            fileobj.write('\n')

        title = f'# Enum {typedef}.\n'
        fileobj.write(title)
        for enum, value in enums.items():
            fileobj.write(f'{enum} = {value}\n')

def write_prototypes(prototypes, callbacks, fileobj):
    doc = '"""Pulseaudio prototypes."""\n\n'
    fileobj.write(doc)

    title = '# Functions.\n'
    fileobj.write(title)
    fileobj.write('functions = ' + pprint.pformat(prototypes))
    fileobj.write('\n\n')

    title = '# Callbacks.\n'
    fileobj.write(title)
    fileobj.write('callbacks = ' + pprint.pformat(callbacks))
    fileobj.write('\n')

def main(stdout=False):
    if shutil.which('gcc') is None:
        print('GNU gcc is required.', file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(PULSEAUDIO_H):
        print(f'*** Error: {PULSEAUDIO_H} does not exist.', file=sys.stderr)
        sys.exit(1)

    typedef_enums = parse_enums(PULSEAUDIO_H, ENUM_TYPEDEFS)
    prototypes = parse_prototypes(PULSEAUDIO_H, function_regexps())
    callbacks = parse_callbacks(PULSEAUDIO_H, callback_regexps())

    if stdout:
        write_header(typedef_enums, sys.stdout)
        write_prototypes(prototypes, callbacks, sys.stdout)
    else:
        with open(PULSEAUDIO_H_PY, 'w') as f:
            write_header(typedef_enums, f)
        with open(PROTOTYPES_PY, 'w') as f:
            write_prototypes(prototypes, callbacks, f)
        print(f"Files '{PULSEAUDIO_H_PY}' and '{PROTOTYPES_PY}'"
              f' have been created.')

if __name__ == '__main__':
    main()
