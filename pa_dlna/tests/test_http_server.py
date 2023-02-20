"""Http server test cases."""

import re
import asyncio
import logging
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, skip_loop_iterations
from .track_processes import (unix_socket_path, PAREC_PATH_ENV,
                              ENCODER_PATH_ENV, BLKSIZE)
from .pulsectl import use_pulsectl_stubs
from ..upnp.tests import find_in_logs, search_in_logs
from ..config import UserConfig
from ..encoders import select_encoder, FFMpegEncoder, L16Encoder
from ..http_server import HTTPServer, Track

with use_pulsectl_stubs(['pa_dlna.pulseaudio', 'pa_dlna.pa_dlna']) as modules:
    pulseaudio, pa_dlna = modules

async def run_curl(url, http_version='http1.1'):
    curl_cmd = ['curl', '--silent', '--show-error', f'--{http_version}', url]
    proc = await asyncio.create_subprocess_exec(*curl_cmd,
                                          stdin=asyncio.subprocess.DEVNULL,
                                          stdout=asyncio.subprocess.PIPE,
                                          stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if stderr and 0:
        print(f'CURL stderr: {stderr.decode().strip()}')
    return proc.returncode, len(stdout.decode())

async def new_renderer(mime_type):
    renderer = Renderer(ControlPoint(), mime_type)
    await renderer.setup()
    return renderer

async def play_track(mime_type, transactions, completed=None):
    env_path = PAREC_PATH_ENV if 'l16' in mime_type else ENCODER_PATH_ENV

    with unix_socket_path(env_path) as sock_path:
        renderer = await new_renderer(mime_type)

        # Start the http server.
        control_point = renderer.control_point
        http_server = HTTPServer(control_point, renderer.local_ipaddress,
                                 control_point.port)
        http_server.allow_from(renderer.root_device.peer_ipaddress)
        asyncio.create_task(http_server.run(), name='http_server')

        # Start the AF_UNIX socket server.
        server = UnixSocketServer(sock_path, transactions, completed)
        asyncio.create_task(server.run(), name='socket server')

        # Start curl.
        # Skip some asyncio loop iterations to have the http server ready
        # before starting curl.
        await skip_loop_iterations(10)
        curl_task = asyncio.create_task(run_curl(renderer.current_uri),
                                        name='curl')

        # Wait for the last chunk of data to be written to the pipe read by
        # Track.write_track().
        if completed is not None:
            await asyncio.wait_for(completed, timeout=1)
        return curl_task, renderer

class Sink:
    monitor_source_name = 'monitor source name'

class NullSink:
    sink = Sink()

class Renderer(pa_dlna.DLNATestDevice):
    def __init__(self, control_point, mime_type):
        super().__init__(control_point, mime_type)
        self.nullsink = NullSink()

        self.set_current_uri()
        control_point.renderers.add(self)

    async def setup(self):
        await self.select_encoder(self.root_device.udn)
        if self.encoder is not None:
            self.encoder._pgm = 'pa_dlna/tests/encoder'
            self.encoder.args = ''

    async def disable_for(self, *, period):
        pass

    async def disable_root_device(self):
        pass

class ControlPoint:
    def __init__(self):
        self.port = 8080
        self.renderers = set()
        self.parec_pgm = 'pa_dlna/tests/parec'

        # Do not read a local pa_dlna.conf.
        with mock.patch('builtins.open', mock.mock_open()) as m_open:
            m_open.side_effect = FileNotFoundError()
            self.config = UserConfig()

    def abort(self, msg):
        pass

    async def close(self, msg=None):
        pass

class UnixSocketServer:
    """Accept connections on an AF_UNIX socket."""

    def __init__(self, path, transactions, completed):
        self.path = path
        self.transactions = transactions
        self.completed = completed

    async def client_connected(self, reader, writer):
        """Handle request/expect transactions.

        The first element of 'transactions' is either:
            - 'ignore'
            - 'dont_sleep'
            - 'FFMpegEncoder'
            - an Exception class name
        The following elements are the number of bytes to write to stdout.
        """

        first = self.transactions[0]
        assert (first in ('ignore', 'dont_sleep', 'FFMpegEncoder') or
                isinstance(eval(first + '()'), Exception))
        writer.write(first.encode())
        resp = await reader.read(1024)
        assert resp == b'Ok'

        for count in self.transactions[1:]:
            assert isinstance(count, int)
            writer.write(str(count).encode())
            await writer.drain()

            await reader.read(1024)

        writer.close()
        await writer.wait_closed()
        self.completed.set_result(True)

    async def run(self):
        aio_server = await asyncio.start_unix_server(self.client_connected,
                                                     self.path)
        async with aio_server:
            await aio_server.serve_forever()

@requires_resources('curl')
class HttpServer(IsolatedAsyncioTestCase):
    """Http server test cases."""

    async def test_play_mp3(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   completed)

            assert not isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.stop_track()
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_play_aiff(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track('audio/aiff', transactions,
                                                   completed)

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
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track('audio/aiff', transactions,
                                                   completed)

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
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track(mime_type, transactions,
                                                   completed)
            assert isinstance(renderer.encoder, L16Encoder)

            await skip_loop_iterations(10)
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_close_session(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   completed)

            await renderer.stream_sessions.close_session()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_partial_read(self):
        # Check use of IncompleteReadError in Track.write_track().
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            data_size = 16 * BLKSIZE + 1
            transactions = ['dont_sleep', data_size]
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   completed)

            await skip_loop_iterations(10)
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, data_size)

    async def test_ConnectionError(self):
        with mock.patch.object(Track, 'write_track') as wtrack,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            wtrack.side_effect = ConnectionError()
            curl_task, renderer = await play_track('audio/mp3',
                                                   ['ignore', BLKSIZE])
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
                                                   ['ignore', BLKSIZE])
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                                       re.compile('RuntimeError\(\)')))

    async def test_disable_with_encoder(self):
        with mock.patch.object(Renderer, 'disable_root_device') as disable,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track('audio/mp3', ['OSError'],
                                                   completed)
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
            loop = asyncio.get_running_loop()
            completed = loop.create_future()
            curl_task, renderer = await play_track(mime_type, ['OSError'],
                                                   completed)
            await skip_loop_iterations(10)
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
