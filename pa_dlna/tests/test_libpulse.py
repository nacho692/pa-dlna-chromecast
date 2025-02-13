"""Testing calls to libpulse.libpulse.LibPulse methods."""

import asyncio
import logging
import re
from unittest import IsolatedAsyncioTestCase

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, search_in_logs
from ..pulseaudio import Pulse

logger = logging.getLogger('libpulse tests')

class SinkInput:
    def __init__(self, client):
        self.client = client

class ControlPoint:
    def __init__(self):
        self.clients_uuids = None
        self.applications = None
        self.start_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        self.test_end = loop.create_future()

    async def close(self):
        pass

    async def dispatch_event(self, event):
        pass

@requires_resources('libpulse')
class LibPulseTests(IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.control_point = ControlPoint()
        self.pulse = Pulse(self.control_point)
        asyncio.create_task(self.pulse.run())

        # Wait for the connection to PulseAudio/Pipewire to be ready.
        await self.control_point.start_event.wait()

    async def asyncTearDown(self):
        # Terminate the self.pulse.run() asyncio task.
        self.control_point.test_end.set_result(True)

    async def test_get_client(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            # Get the list of the current clients.
            lp = self.pulse.lib_pulse
            clients = await lp.pa_context_get_client_info_list()
            logger.debug(f'clients '
                         f'{dict((cl.name, cl.index) for cl in clients)}')

            # Find the last client index.
            max_index = 0
            indexes = [client.index for client in clients]
            if indexes:
                max_index = max(indexes)
            logger.debug(f'max_index: {max_index}')

            # Use an invalid client index.
            sink_input = SinkInput(max_index + 10)
            client = await self.pulse.get_client(sink_input)
            self.assertEqual(client, None)

        self.assertTrue(search_in_logs(m_logs.output, 'pulse',
                                    re.compile(r'LibPulseOperationError')))
