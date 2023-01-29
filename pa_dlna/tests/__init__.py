import os
import sys
import contextlib
import logging
import subprocess
import unittest
import functools

from ..upnp.tests import load_ordered_tests

if sys.version_info >= (3, 9):
    functools_cache = functools.cache
else:
    functools_cache = functools.lru_cache

def _id(obj):
    return obj

@functools_cache
def requires_resources(resources):
    """Skip the test when one of the resource is not available.

    'resources' is a string or a tuple instance (MUST be hashable).
    """

    resources = [resources] if isinstance(resources, str) else resources
    for res in resources:
        try:
            if res == 'os.devnull':
                # Check that os.devnull is writable.
                with open(os.devnull, 'w'):
                    pass
            elif res == 'pulseaudio':
                # Check that pulseaudio is running.
                subprocess.run(['pactl', 'info'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True)
            else:
                # Otherwise check that the module can be imported.
                exec(f'import {res}')
        except Exception:
            return unittest.skip(f"'{res}' is not available")
    else:
        return _id

class BaseTestCase(unittest.TestCase):
    def setUp(self):
        # Redirect stderr to os.devnull.
        self.stack = contextlib.ExitStack()
        f = self.stack.enter_context(open(os.devnull, 'w'))
        self.stack.enter_context(contextlib.redirect_stderr(f))

    def tearDown(self):
        self.stack.close()

        # Remove the root logger handler set up by init.setup_logging().
        root = logging.getLogger()
        for hdl in root.handlers:
            root.removeHandler(hdl)
