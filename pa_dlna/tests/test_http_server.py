"""Http server test cases."""

import re
import asyncio
import logging
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, find_in_logs, search_in_logs
from .streams import BLKSIZE, run_curl, new_renderer, play_track, Renderer
from ..encoders import FFMpegEncoder, L16Encoder
from ..http_server import HTTPServer, Track

@requires_resources('curl')
class Http_Server(IsolatedAsyncioTestCase):
    """Http server test cases."""

    async def test_play_mp3(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   logs=m_logs)

            assert not isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.stop_track()
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_play_aiff(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/aiff', transactions,
                                                   logs=m_logs)

            assert isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.stop_track()
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_play_aiff_255(self):
        # Test that an FFMpegEncoder encoder exiting with an exit_status of
        # 255 is reported as 'Terminated'.
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['FFMpegEncoder', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/aiff', transactions,
                                                   logs=m_logs)

            assert isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.processes.encoder_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                'Exit status of encoder process: Terminated'))
        self.assertTrue(find_in_logs(m_logs.output, 'encoder',
                                'encoder stub return_code: 255'))

    async def test_play_l16(self):
        # Test playing track with no encoder.
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            mime_type = 'audio/l16;rate=44100;channels=2'
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track(mime_type, transactions,
                                                   logs=m_logs)

            assert isinstance(renderer.encoder, L16Encoder)
            await renderer.stream_sessions.processes.parec_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_close_session(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   logs=m_logs)

            await renderer.stream_sessions.close_session()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_partial_read(self):
        # Check use of IncompleteReadError in Track.write_track().
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            data_size = 16 * BLKSIZE + 1
            transactions = ['dont_sleep', data_size]
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   logs=m_logs)

            await renderer.stream_sessions.processes.encoder_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_ConnectionError(self):
        with mock.patch.object(Track, 'write_track') as wtrack,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            wtrack.side_effect = ConnectionError()
            curl_task, renderer = await play_track('audio/mp3',
                ['ignore', BLKSIZE], wait_for_completion=False, logs=m_logs)
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                        re.compile('HTTP socket is closed: ConnectionError')))

    async def test_Exception(self):
        with mock.patch.object(Track, 'write_track') as wtrack,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            wtrack.side_effect = RuntimeError()
            curl_task, renderer = await play_track('audio/mp3',
                ['ignore', BLKSIZE], wait_for_completion=False, logs=m_logs)
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                                       re.compile('RuntimeError\(\)')))

    async def test_disable_with_encoder(self):
        with mock.patch.object(Renderer, 'disable_root_device') as disable,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            curl_task, renderer = await play_track('audio/mp3', ['OSError'],
                                                   logs=m_logs)
            returncode, length = await curl_task

            await renderer.stream_sessions.processes.encoder_task
            disable.assert_called_once()

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                     'Exit status of encoder process: 1'))

    async def test_disable_with_parec(self):
        with mock.patch.object(Renderer, 'disable_root_device') as disable,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            mime_type = 'audio/l16;rate=44100;channels=2'
            curl_task, renderer = await play_track(mime_type, ['OSError'],
                                                   logs=m_logs)

            await renderer.stream_sessions.processes.parec_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task
            disable.assert_called_once()

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                     'Exit status of parec process: 1'))

    async def test_not_allowed(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await new_renderer('audio/mp3')

            # Start the http server.
            control_point = renderer.control_point
            http_server = HTTPServer(control_point, renderer.local_ipaddress,
                                     control_point.port)
            asyncio.create_task(http_server.run(), name='http_server')

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertNotEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                                    re.compile('Discarded.*not allowed')))

    async def test_renderer_not_found(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await new_renderer('audio/mp3')

            # Start the http server.
            control_point = renderer.control_point
            http_server = HTTPServer(control_point, renderer.local_ipaddress,
                                     control_point.port)
            http_server.allow_from(renderer.root_device.peer_ipaddress)
            asyncio.create_task(http_server.run(), name='http_server')

            # Start curl.
            curl_task = asyncio.create_task(run_curl(
                                                renderer.current_uri + 'fff'))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'util',
                            re.compile('Cannot find a matching renderer')))

    async def test_http_version(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await new_renderer('audio/mp3')

            # Start the http server.
            control_point = renderer.control_point
            http_server = HTTPServer(control_point, renderer.local_ipaddress,
                                     control_point.port)
            http_server.allow_from(renderer.root_device.peer_ipaddress)
            asyncio.create_task(http_server.run(), name='http_server')

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri,
                                                     http_version='http1.0'))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'util',
                                    re.compile('HTTP Version Not Supported')))

    async def test_is_playing(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await new_renderer('audio/mp3')
            renderer.stream_sessions.is_playing = True

            # Start the http server.
            control_point = renderer.control_point
            http_server = HTTPServer(control_point, renderer.local_ipaddress,
                                     control_point.port)
            http_server.allow_from(renderer.root_device.peer_ipaddress)
            asyncio.create_task(http_server.run(), name='http_server')

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'util',
            re.compile('Cannot start DLNATest.* stream .already running')))

    async def test_None_nullsink(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await new_renderer('audio/mp3')
            renderer.nullsink = None

            # Start the http server.
            control_point = renderer.control_point
            http_server = HTTPServer(control_point, renderer.local_ipaddress,
                                     control_point.port)
            http_server.allow_from(renderer.root_device.peer_ipaddress)
            asyncio.create_task(http_server.run(), name='http_server')

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'util',
                            re.compile('DLNATest.* temporarily disabled')))

if __name__ == '__main__':
    unittest.main(verbosity=2)
