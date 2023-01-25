import os
import sys
import asyncio
import contextlib
import functools
import subprocess
import logging
import unittest
from unittest import mock

from ..upnp import UPnPControlPoint

if sys.version_info >= (3, 9):
    functools_cache = functools.cache
else:
    functools_cache = functools.lru_cache

MSEARCH_PORT = 9999

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

def load_ordered_tests(loader, standard_tests, pattern):
    """Keep the tests in the order they were declared in the class.

    Thanks to https://stackoverflow.com/a/62073640
    """

    ordered_cases = []
    for test_suite in standard_tests:
        ordered = []
        for test_case in test_suite:
            test_case_type = type(test_case)
            method_name = test_case._testMethodName
            testMethod = getattr(test_case, method_name)
            line = testMethod.__code__.co_firstlineno
            ordered.append( (line, test_case_type, method_name) )
        ordered.sort()
        for line, case_type, name in ordered:
            ordered_cases.append(case_type(name))
    return unittest.TestSuite(ordered_cases)

def find_in_logs(logs, logger, msg):
    """Return True if 'msg' from 'logger' is in 'logs'."""

    for log in (log.split(':', maxsplit=2) for log in logs):
        if len(log) == 3 and log[1] == logger and log[2] == msg:
            return True
    return False

def search_in_logs(logs, logger, matcher):
    """Return True if the matcher's pattern is found in a message in 'logs'."""

    for log in (log.split(':', maxsplit=2) for log in logs):
        if (len(log) == 3 and log[1] == logger and
                matcher.search(log[2]) is not None):
            return True
    return False

async def loopback_datagrams(datagrams, patch_method=None, setup=None):
    """Loopback datagrams to UPnPControlPoint._process_ssdp.

    datagrams       Either a coroutine that sends datagrams or a list of
                    datagrams to be broadcasted to the UPnP multicast
                    address.
    patch_method    The name of a method of the UPnPControlPoint instance to
                    patch.
    setup           A coroutine to be awaited for before sending the
                    datagrams.
    """

    async def send_datagrams(ip, protocol):
        # 'protocol' is the protocol of the MsearchServerProtocol instance.
        for datagram in datagrams:
            protocol.send_datagram(datagram)

    async def is_called(mock):
        while True:
            await asyncio.sleep(0)
            if mock.called:
                return True

    if asyncio.iscoroutinefunction(datagrams):
        coro = datagrams
    else:
        coro = send_datagrams
    control_point = UPnPControlPoint(['lo'], 3600)
    try:
        with mock.patch.object(control_point,
                               '_ssdp_msearch') as ssdp_msearch:
            if patch_method is not None:
                patcher = mock.patch.object(control_point, patch_method)
                method = patcher.start()

            # Prevent the msearch task to run UPnPControlPoint._ssdp_msearch.
            ssdp_msearch.side_effect = [None]
            if setup is not None:
                await setup(control_point)

            await control_point.open()
            await control_point._notify.startup
            # 'coro' is a coroutine *function*.
            await control_point.msearch_once(coro, port=MSEARCH_PORT)

            if patch_method is not None:
                try:
                    await asyncio.wait_for(is_called(method), 1)
                except asyncio.TimeoutError:
                    raise AssertionError(
                        f'{patch_method}() not called') from None
    finally:
        control_point.close()

    return control_point

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
