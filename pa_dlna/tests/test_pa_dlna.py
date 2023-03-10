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

from . import find_in_logs, search_in_logs
from .streams import pulseaudio, pa_dlna
from .streams import set_control_point as _set_control_point
from .pulsectl import PulseAsync
from ..init import ControlPointAbortError
from ..upnp.upnp import UPnPRootDevice, QUEUE_CLOSED, UPnPControlPoint
from ..upnp.tests import min_python_version

AVControlPoint = pa_dlna.AVControlPoint
Renderer = pa_dlna.Renderer

async def wait_for(awaitable, timeout=2):
    """Work around of the asyncio.wait_for() bug, new in Python 3.9.

    Bug summary: In some cases asyncio.wait_for() does not raise TimeoutError
    although the future has been cancelled after the timeout.
    """

    bug = sys.version_info > (3, 8)
    if bug:
        start = time.time()
    res = await asyncio.wait_for(awaitable, timeout=timeout)
    if bug and time.time() - start >= timeout - 0.1:
        raise asyncio.TimeoutError('*** asyncio.wait_for() BUG:'
                                ' failed to raise TimeoutError')
    return res

def set_control_point(control_point):
    _set_control_point(control_point)
    loop = asyncio.get_running_loop()
    control_point.test_end = loop.create_future()

def set_no_encoder(control_point):
    set_control_point(control_point)
    control_point.config.encoders = {}

class RootDevice(UPnPRootDevice):


    def __init__(self, upnp_control_point, mime_type='audio/mp3',
                                           device_type=True):
        self.mime_type = mime_type
        match = re.match(r'audio/([^;]+)', mime_type)
        name = match.group(1)
        self.modelName = f'RootDevice_{name}'
        self.friendlyName = self.modelName
        self.udn = pa_dlna.get_udn(name.encode())

        assert device_type in (None, True, False)
        if device_type:
            self.deviceType = f'{pa_dlna.MEDIARENDERER}1'
        elif device_type is False:
            self.deviceType = 'some device type'

        loopback = '127.0.0.1'
        super().__init__(upnp_control_point, self.udn, loopback, loopback,
                         None, 3600)

class PaDlnaTestCase(IsolatedAsyncioTestCase):
    async def run_control_point(self, handle_pulse_event,
                                set_control_point=set_control_point,
                                test_devices=[],
                                has_parec=True):

        _which = shutil.which
        def which(arg):
            if arg == 'parec':
                return True if has_parec else None
            else:
                return _which(arg)

        # When 'test_end' is done, the task running
        # control_point.run_control_point() is cancelled by the Pulse task
        # closing the AVControlPoint instance 'control_point'.
        with mock.patch.object(Renderer,
                               'handle_pulse_event', handle_pulse_event),\
                mock.patch.object(shutil, 'which', which),\
                self.assertLogs(level=logging.DEBUG) as m_logs:

            control_point = AVControlPoint(nics='lo', port=8080, ttl=2,
                                           msearch_interval=60,
                                           test_devices=test_devices)
            set_control_point(control_point)
            PulseAsync.add_sink_inputs([])

            try:
                return_code = await wait_for(
                                        control_point.run_control_point())
            except asyncio.TimeoutError:
                logs = ('\n'.join(l for l in m_logs.output if
                                  ':asyncio:' not in l))
                logs = None if not logs else '\n' + logs
                self.fail(f'TimeoutError with logs: {logs}')

        return return_code, m_logs

class DLNAControlPoint(PaDlnaTestCase):
    """The control point test cases."""

    async def test_no_encoder(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                            test_devices=['audio/mp3'],
                                            set_control_point=set_no_encoder)
        self.assertTrue(isinstance(return_code, RuntimeError))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                                       re.compile('No encoder is available')))

    async def test_no_parec(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                                test_devices=['audio/mp3'],
                                                has_parec=False)
        self.assertTrue(isinstance(return_code, RuntimeError))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                        re.compile("'parec' program cannot be found")))

    @min_python_version((3, 9))
    async def test_cancelled(self):
        async def handle_pulse_event(renderer):
            renderer.control_point.curtask.cancel('foo')
            await asyncio.sleep(0)

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                                test_devices=['audio/mp3'])
        self.assertTrue(isinstance(return_code, asyncio.CancelledError))
        self.assertTrue(find_in_logs(logs.output, 'pa-dlna',
                                     "Main task got: CancelledError('foo')"))

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

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                                test_devices=['audio/mp3'])

        self.assertTrue(isinstance(return_code, asyncio.CancelledError))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                                       re.compile('Got SIGINT or SIGTERM')))

class DLNARenderer(PaDlnaTestCase):
    """The renderer test cases using run_control_point()."""

    async def test_register_renderer(self):
        async def handle_pulse_event(renderer):
            renderer.control_point.test_end.set_result(True)
            raise OSError('foo')

        return_code, logs = await self.run_control_point(handle_pulse_event,
                                                test_devices=['audio/mp3'])

        self.assertTrue(return_code is None,
                        msg=f'return_code: {return_code}')
        _logs = '\n'.join(l for l in logs.output if ':ASYNCIO:' not in l)
        self.assertTrue(find_in_logs(logs.output, 'pa-dlna', "OSError('foo')"),
                        msg=_logs)  # Print the logs if the assertion fails.
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                    re.compile('New DLNATest_.* renderer with Mp3Encoder')))

    async def test_unknown_encoder(self):
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

    async def test_bad_encoder_unload_module(self):
        async def handle_pulse_event(renderer):
            await asyncio.sleep(0)  # Never reached

        def disable(control_point, root_device, name=None):
            # Do not close renderers in AVControlPoint.close().
            control_point.renderers = set()
            control_point.test_end.set_result(True)

        # Check that the 'module-null-sink' module of a renderer whose encoder
        # is not found, is unloaded.
        with mock.patch.object(AVControlPoint, 'disable_root_device',
                               disable):
            return_code, logs = await self.run_control_point(
                            handle_pulse_event, test_devices=['audio/foo'])

        self.assertEqual(return_code, None)
        self.assertTrue(search_in_logs(logs.output, 'pulse',
                        re.compile('Unload null-sink module DLNATest_foo')))

class PatchGetNotificationTests(IsolatedAsyncioTestCase):
    """Test cases using patch_get_notification()."""

    def setUp(self):
        self.upnp_control_point = UPnPControlPoint([], 60)
        self.control_point = AVControlPoint(nics=['lo'], port=8080)
        self.control_point.upnp_control_point = self.upnp_control_point

        # PulseAsync must be instantiated after the call to the
        # add_sink_inputs() class method.
        PulseAsync.add_sink_inputs([])
        self.control_point.pulse = pulseaudio.Pulse(self.control_point)
        self.control_point.pulse.pulse_ctl = PulseAsync('pa-dlna')

    async def patch_get_notification(self, notifications=[], alive_count=0):
        async def handle_pulse_event(renderer):
            # Wrapper around Renderer.handle_pulse_event to trigger the
            # 'test_end' future after 'alive_count' calls to this method from
            # new renderers.
            nonlocal handle_pulse_event_called
            handle_pulse_event_called += 1
            if handle_pulse_event_called == alive_count:
                renderer.control_point.test_end.set_result(True)
            await _handle_pulse_event(renderer)

        _handle_pulse_event = Renderer.handle_pulse_event
        handle_pulse_event_called = 0
        set_control_point(self.control_point)

        with mock.patch.object(self.upnp_control_point,
                               'get_notification') as get_notif,\
                mock.patch.object(Renderer, 'soap_action',
                                  pa_dlna.DLNATestDevice.soap_action),\
                mock.patch.object(Renderer, 'handle_pulse_event',
                                  handle_pulse_event),\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            notifications.append(QUEUE_CLOSED)
            get_notif.side_effect = notifications
            await self.control_point.handle_upnp_notifications()
            if alive_count != 0:
                try:
                    await wait_for(self.control_point.test_end)
                except asyncio.TimeoutError:
                    logs = ('\n'.join(l for l in m_logs.output if
                                      ':asyncio:' not in l))
                    logs = None if not logs else '\n' + logs
                    self.fail(f'TimeoutError with logs: {logs}')

        return m_logs

    async def test_alive(self):
        root_device = RootDevice(self.upnp_control_point)
        logs = await self.patch_get_notification([('alive', root_device)],
                                                 alive_count=1)

        self.assertEqual(len(self.control_point.renderers), 1)
        renderer = self.control_point.renderers.pop()
        self.assertEqual(renderer.root_device, root_device)
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                re.compile('New RootDevice_mp3.* renderer with Mp3Encoder')))

    async def test_missing_deviceType(self):
        root_device = RootDevice(self.upnp_control_point, device_type=None)
        logs = await self.patch_get_notification([('alive', root_device)],
                                                 alive_count=0)

        self.assertEqual(len(self.control_point.renderers), 0)
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                re.compile('missing deviceType')))
        self.assertTrue(search_in_logs(logs.output, 'upnp',
                re.compile('Disable the UPnPRootDevice .* permanently')))

    async def test_not_MediaRenderer(self):
        root_device = RootDevice(self.upnp_control_point, device_type=False)
        logs = await self.patch_get_notification([('alive', root_device)],
                                                 alive_count=0)

        self.assertEqual(len(self.control_point.renderers), 0)
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                re.compile('not a MediaRenderer')))
        self.assertTrue(search_in_logs(logs.output, 'upnp',
                re.compile('Disable the UPnPRootDevice .* permanently')))

    async def test_byebye(self):
        root_device = RootDevice(self.upnp_control_point)
        mpeg_root_device = RootDevice(self.upnp_control_point,
                                      mime_type='audio/mpeg')

        # Using two 'byebye' notifications to emulate the behavior of the root
        # device that sends one after having been closed by Renderer.close().
        logs = await self.patch_get_notification([('alive', root_device),
                                                  ('byebye', root_device),
                                                  ('byebye', root_device),
                                                  ('alive', mpeg_root_device)
                                                  ],
                                                 alive_count=2)

        self.assertEqual(len(self.control_point.renderers), 1)
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                re.compile("Got 'byebye' notification")))
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                re.compile('Close RootDevice_mp3')))
        self.assertTrue(search_in_logs(logs.output, 'pulse',
                re.compile('Unload null-sink module RootDevice_mp3')))

    async def test_disabled_root_device(self):
        root_device = RootDevice(self.upnp_control_point)
        mpeg_root_device = RootDevice(self.upnp_control_point,
                                      mime_type='audio/mpeg')

        # Capture the logs (and ignore them) to avoid them being printed on
        # stderr.
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            self.control_point.disable_root_device(root_device)

        logs = await self.patch_get_notification([('alive', root_device),
                                                  ('alive', mpeg_root_device)
                                                  ],
                                                 alive_count=1)

        self.assertEqual(len(self.control_point.renderers), 1)
        self.assertTrue(search_in_logs(logs.output, 'pa-dlna',
                                re.compile('Ignore disabled UPnPRootDevice')))
    async def test_local_ipaddress(self):
        root_device = RootDevice(self.upnp_control_point)
        root_device.local_ipaddress = None

        logs = await self.patch_get_notification([('alive', root_device),],
                                                 alive_count=1)

        self.assertEqual(len(self.control_point.renderers), 1)
        renderer = self.control_point.renderers.pop()
        self.assertEqual(renderer.local_ipaddress, '127.0.0.1')

    async def test_no_local_ipaddress(self):
        root_device = RootDevice(self.upnp_control_point)
        root_device.local_ipaddress = None
        self.control_point.nics = ['an unknown network interface cards']

        logs = await self.patch_get_notification([('alive', root_device),],
                                                 alive_count=0)

        self.assertEqual(len(self.control_point.renderers), 0)
        self.assertTrue(find_in_logs(logs.output, 'pa-dlna',
                'Ignored: 127.0.0.1 does not belong to one of the known'
                ' network interfaces'))
