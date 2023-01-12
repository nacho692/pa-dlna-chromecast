"""Forward pulseaudio streams to DLNA devices."""

import sys

__version__ = '0.1'
MIN_PYTHON_VERSION = (3, 8)

def py_version():
    # This function is patched in the 'test_python_version' TestCase.
    return sys.version_info[:2]

def check_python_version():
    version = py_version()
    if version < MIN_PYTHON_VERSION:
        print(f'error: the python version must be at least'
              f' {MIN_PYTHON_VERSION}', file=sys.stderr)
        sys.exit(1)
    return version

VERSION = check_python_version()
