"""Forward pulseaudio streams to DLNA devices."""

import sys

__version__ = '0.1'
MIN_PYTHON_VERSION = (3, 8)

VERSION = sys.version_info
if VERSION[:2] < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)
