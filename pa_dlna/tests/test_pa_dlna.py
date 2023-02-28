"""pa_dlna test cases."""

import re
import sys
import asyncio
import signal
import time
import shutil
import logging
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, find_in_logs, search_in_logs
from .streams import pulseaudio, pa_dlna
from .streams import set_control_point as _set_control_point
from .pulsectl import PulseAsync
from ..init import ControlPointAbortError
from ..upnp.tests import min_python_version

AVControlPoint = pa_dlna.AVControlPoint
Renderer = pa_dlna.Renderer

def set_control_point(control_point):
    _set_control_point(control_point)
    loop = asyncio.get_running_loop()
    control_point.test_end = loop.create_future()

def set_no_encoder(control_point):
    set_control_point(control_point)
    control_point.config.encoders = {}

class DlnaControlPoint(IsolatedAsyncioTestCase):
    """The control point test cases."""

    async def run_control_point(self, handle_pulse_event,
                                set_control_point=set_control_point,
                                test_devices=['audio/mp3']):
        async def handle_upnp_notifications(control_point):
            await control_point.test_end

        with mock.patch.object(AVControlPoint, 'handle_upnp_notifications',
                               handle_upnp_notifications),\
                mock.patch.object(Renderer,
                                  'handle_pulse_event', handle_pulse_event),\
                self.assertLogs(level=logging.DEBUG) as m_logs:

            control_point = AVControlPoint(nics='lo', port=8080, ttl=2,
                                           msearch_interval=60,
                                           test_devices=test_devices)
            set_control_point(control_point)

            try:
                PulseAsync.add_sink_inputs([])
                waiting_time = 2

                # Work around of the wait_for() bug that is new in Python 3.9.
                bug = sys.version_info > (3, 8)
                if bug:
                    start = time.time()
                return_code = await asyncio.wait_for(
                                        control_point.run_control_point(),
                                        timeout=waiting_time)
                if bug and time.time() - start >= waiting_time - 0.1:
                    raise asyncio.TimeoutError('*** asyncio.wait_for() BUG:'
                                            ' failed to raise TimeoutError')

            except asyncio.TimeoutError:
                logs = ('\n'.join(l for l in m_logs.output if
                                  ':asyncio:' not in l))
                logs = None if not logs else '\n' + logs
                self.fail(f'TimeoutError with logs: {logs}')

        return return_code, m_logs

    async def test_register_renderer(self):
        async def handle_pulse_event(renderer):
            renderer.control_point.test_end.set_result(True)
            raise OSError('foo')

        return_code, logs = await self.run_control_point(handle_pulse_event)
        self.assertTrue(return_code is None)
        self.assertTrue(find_in_logs(logs.output, 'pa-dlna', "OSError('foo')"))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                    re.compile('New DLNATest_.* renderer with Mp3Encoder')))

    async def test_no_encoder(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                            set_control_point=set_no_encoder)
        self.assertTrue(isinstance(return_code, RuntimeError))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                                       re.compile('No encoder is available')))

    async def test_no_parec(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)

        def which(arg):
            return None

        with mock.patch.object(shutil, 'which', which):
            return_code, logs = await self.run_control_point(handle_pulse_event)

        self.assertTrue(isinstance(return_code, RuntimeError))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                        re.compile("'parec' program cannot be found")))

    @min_python_version((3, 9))
    async def test_cancelled(self):
        async def handle_pulse_event(renderer):
            renderer.control_point.curtask.cancel('foo')

        return_code, logs = await self.run_control_point(handle_pulse_event)
        self.assertTrue(isinstance(return_code, asyncio.CancelledError))
        self.assertTrue(find_in_logs(logs.output, 'pa-dlna',
                                     "Main task got: CancelledError('foo')"))

    async def test_disable_root_device(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)  # Never reached

        def disable(control_point, root_device, name=None):
            logger = logging.getLogger('foo')
            logger.warning(f'Disable the {name} device permanently')
            control_point.test_end.set_result(True)

        with mock.patch.object(AVControlPoint, 'disable_root_device',
                               disable):
            return_code, logs = await self.run_control_point(
                            handle_pulse_event, test_devices=['audio/foo'])

        self.assertEqual(return_code, None)
        self.assertTrue(search_in_logs(logs.output, 'foo',
                    re.compile('Disable the DLNATest_.* device permanently')))

    async def test_abort(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)  # Avoid infinite loop.

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                test_devices=['audio/mp3', 'audio/mp3'])

        self.assertTrue(type(return_code), ControlPointAbortError)
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                re.compile('Two DLNA devices registered with the same name')))

    @min_python_version((3, 9))
    async def test_SIGINT(self):
        async def handle_pulse_event(renderer):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0)  # Avoid infinite loop.

        return_code, logs = await self.run_control_point(handle_pulse_event)

        self.assertTrue(isinstance(return_code, asyncio.CancelledError))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                                       re.compile('Got SIGINT or SIGTERM')))

@requires_resources('curl')
class DlnaRenderer(IsolatedAsyncioTestCase):
    """The renderer test cases."""
