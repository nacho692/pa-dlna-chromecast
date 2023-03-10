"""Pulseaudio test cases."""

import re
import asyncio
import contextlib
import logging
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import find_in_logs, search_in_logs
from .streams import pulseaudio, pa_dlna
from .pulsectl import (use_pulsectl_stubs, SinkInput, Event,
                       PulseEventTypeEnum, PulseEventMaskEnum,
                       PulseDisconnected, PulseAsync)

class Renderer(pa_dlna.DLNATestDevice):
    def __init__(self, control_point, mime_type, results=None):
        super().__init__(control_point, mime_type)
        self.results = results

    async def run(self):
        while True:
            event_tuple = await self.pulse_queue.get()
            if self.results is not None:
                self.results.append(event_tuple)
            evt, sink, sink_input = event_tuple
            if evt == 'new':
                self.nullsink.sink = sink
                self.nullsink.sink_input = sink_input

class ControlPoint:
    def __init__(self):
        self.start_event = asyncio.Event()
        self.renderers = set()

    def abort(self, msg):
        pass

    async def close(self):
        pass

class Pulseaudio(IsolatedAsyncioTestCase):
    """Pulseaudio test cases."""

    def setUp(self):
        # The Pulse instance to test.
        self.control_point = ControlPoint()
        self.pulse = pulseaudio.Pulse(self.control_point)

    def new_renderer(self, mime_type, results):
        renderer = Renderer(self.control_point, f'audio/{mime_type}',
                            results)
        asyncio.create_task(renderer.run())
        self.control_point.renderers.add(renderer)
        return renderer

    async def test_run_pulse(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            PulseAsync.add_sink_inputs([])
            await self.pulse.run()

        self.assertTrue(find_in_logs(m_logs.output, 'pulse', 'Close pulse'))

    async def test_connect_raise_once(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            PulseAsync.add_sink_inputs([SinkInput('source',
                                            [Event(PulseEventTypeEnum.new)])])
            PulseAsync.do_raise_once = True
            await self.pulse.run(sleep=0)

        self.assertTrue(find_in_logs(m_logs.output, 'pulse',
                                'Waiting to connect to pulseaudio server'))
        self.assertTrue(find_in_logs(m_logs.output, 'pulse', 'Close pulse'))

    async def test_disconnected(self):
        with mock.patch.object(self.pulse, 'dispatch_event') as dispatch,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            PulseAsync.add_sink_inputs([SinkInput('source',
                                            [Event(PulseEventTypeEnum.new)])])
            dispatch.side_effect = PulseDisconnected()
            await self.pulse.run()

        self.assertTrue(find_in_logs(m_logs.output, 'pulse',
                                     'Pulseaudio error: PulseDisconnected()'))

    async def test_register(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            PulseAsync.add_sink_inputs([])
            async with PulseAsync('pa-dlna') as self.pulse.pulse_ctl:
                sink = await self.pulse.register(renderer)
                self.assertTrue(str(self.pulse.pulse_ctl.sinks[1]).startswith(
                                                        'DLNATest_mp3-uuid:'))
                await self.pulse.unregister(sink)

        self.assertTrue(search_in_logs(m_logs.output, 'pulse',
                                    re.compile('Load null-sink module'
                                    ' DLNATest_mp3-uuid:.*\n.*description=')))

    async def test_register_failure(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            PulseAsync.add_sink_inputs([])
            async with PulseAsync('pa-dlna') as self.pulse.pulse_ctl:
                with mock.patch.object(self.pulse.pulse_ctl,
                                       'module_load') as load:
                    load.side_effect = [999]
                    await self.pulse.register(renderer)

        self.assertTrue(search_in_logs(m_logs.output, 'pulse',
                             re.compile('Failed loading DLNATest_mp3-uuid:')))

    async def test_register_twice(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            PulseAsync.add_sink_inputs([])
            async with PulseAsync('pa-dlna') as self.pulse.pulse_ctl:
                with mock.patch.object(self.control_point, 'abort') as abort:
                    await self.pulse.register(renderer)
                    await self.pulse.register(renderer)

        abort.assert_called_once()

    async def test_remove_event(self):
        results = []
        renderer = self.new_renderer('mp3', results)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            sink_input = SinkInput('source', [Event(PulseEventTypeEnum.new),
                                            Event(PulseEventTypeEnum.remove)])
            PulseAsync.add_sink_inputs([sink_input])

            async with PulseAsync('pa-dlna') as self.pulse.pulse_ctl:
                renderer.nullsink = await self.pulse.register(renderer)
                async for event in self.pulse.pulse_ctl.subscribe_events(
                                            PulseEventMaskEnum.sink_input):
                    await self.pulse.dispatch_event(event)
                    await asyncio.sleep(0)

        self.assertTrue(results[0] == ('new', renderer.nullsink.sink,
                                       sink_input))
        self.assertTrue(results[1] == ('remove', None, None))

    async def test_exit_event(self):
        results = []
        mp3_renderer = self.new_renderer('mp3', results)
        mpeg_renderer = self.new_renderer('mpeg', results)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            sink_input = SinkInput('source', [Event(PulseEventTypeEnum.new),
                                            Event(PulseEventTypeEnum.new)])
            PulseAsync.add_sink_inputs([sink_input])

            async with PulseAsync('pa-dlna') as self.pulse.pulse_ctl:
                mp3_renderer.nullsink = await self.pulse.register(
                                                            mp3_renderer)
                async for event in self.pulse.pulse_ctl.subscribe_events(
                                            PulseEventMaskEnum.sink_input):
                    await self.pulse.dispatch_event(event)
                    await asyncio.sleep(0)
                    if mpeg_renderer.nullsink is None:
                        mpeg_renderer.nullsink = await self.pulse.register(
                                                                mpeg_renderer)

        self.assertTrue(results[0] == ('new', mp3_renderer.nullsink.sink, sink_input))
        self.assertTrue(results[1] == ('new', mpeg_renderer.nullsink.sink, sink_input))
        self.assertTrue(results[2] == ('exit', None, None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
