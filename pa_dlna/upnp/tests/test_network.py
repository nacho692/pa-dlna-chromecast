"""Network test cases."""

import re
import asyncio
import logging
import socket
from unittest import mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, BaseTestCase, find_in_logs, search_in_logs
from .. import TEST_LOGLEVEL
from ..upnp import UPnPControlPoint
from ..network import (send_mcast, msearch, http_get, UPnPInvalidHttpError,
                       http_soap, Notify)
from ..util import HTTPRequestHandler

HOST = '127.0.0.1'
PORT = 9999
URL = f'http://{HOST}:{PORT}/MediaRenderer/desc.xml'
NTS_ALIVE = 'NTS: ssdp:alive'
SSDP_NOTIFY = '\r\n'.join([
    'NOTIFY * HTTP/1.1',
    'Host: 239.255.255.250:1900',
    'Content-Length: 0',
    ('Location: ' + URL),
    'Cache-Control: max-age=1800',
    'Server: Linux',
    'NT: upnp:rootdevice',
    '{nts}',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '',
    '',
])
SSDP_ALIVE = SSDP_NOTIFY.format(nts=NTS_ALIVE)

MSEARCH_PORT = PORT
ST_ROOT_DEVICE = 'ST: upnp:rootdevice'
SSDP_MSEARCH = '\r\n'.join([
    'HTTP/1.1 200 OK',
    ('Location: ' + URL),
    'Cache-Control: max-age=1800',
    'Content-Length: 0',
    'Server: Linux',
    'EXT:',
    '{st}',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '', '',
])
MSEARCH_RESPONSE = SSDP_MSEARCH.format(st=ST_ROOT_DEVICE)

NOT_FOUND_REASON = 'A dummy reason'
NOT_FOUND = '\r\n'.join([
    f'HTTP/1.1 404 {NOT_FOUND_REASON}',
    'Content-Length: 0',
    '', '',
])

async def loopback(datagrams, wait_for_termination=False, setup=None):
    """Loopback datagrams to the notify or msearch task.

    'datagrams' is either a coroutine that send datagrams or a list of
    datagrams to be broadcasted to the UPnP multicast address.
    'setup' is a coroutine to be awaited for before sending the datagrams.
    """

    async def send_datagrams(ip, protocol):
        # 'protocol' is the protocol of the MsearchServerProtocol instance.
        for datagram in datagrams:
            protocol.send_datagram(datagram)

    async def termination():
        while True:
            await asyncio.sleep(0)
            if create.called or remove.called:
                break

    if asyncio.iscoroutinefunction(datagrams):
        coro = datagrams
    else:
        coro = send_datagrams
    control_point = UPnPControlPoint(['lo'], 3600)
    try:
        with mock.patch.object(control_point,
                               '_create_root_device') as create,\
                mock.patch.object(control_point,
                               '_remove_root_device') as remove,\
                mock.patch.object(control_point,
                               '_ssdp_msearch') as ssdp_msearch:

            # Prevent the msearch task to run UPnPControlPoint._ssdp_msearch.
            ssdp_msearch.side_effect = [None]
            if setup is not None:
                await setup(control_point)

            await control_point.open()
            await control_point._notify.startup
            await control_point._one_shot_msearch(coro, port=MSEARCH_PORT)

            if wait_for_termination:
                try:
                    await asyncio.wait_for(termination(), 1)
                except asyncio.TimeoutError:
                    raise AssertionError('_create_root_device() and '
                                '_remove_root_device() not called') from None
    finally:
        control_point.close()

    return control_point

class HTTPServer:
    def __init__(self, body, content_length=None, start_line=None):
        self.body = body.encode()
        self.body_length = len(self.body)
        content_length = (self.body_length if content_length is None else
                          content_length)

        header = ['HTTP/1.1 200 OK' if start_line is None else
                  start_line]
        header.append(f'Content-Length: {content_length}')
        header.extend(['', ''])
        self.header = '\r\n'.join(header).encode('latin-1')

        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    async def client_connected(self, reader, writer):
        """Handle an HTTP GET request and return the response."""

        peername = writer.get_extra_info('peername')
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()

            # Write the response.
            writer.write(self.header)
            if self.body_length:
                writer.write(self.body)
        finally:
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    async def run(self):
        aio_server = await asyncio.start_server(self.client_connected,
                                                HOST, PORT)
        async with aio_server:
            self.startup.set_result(None)
            await aio_server.serve_forever()

@requires_resources('os.devnull')
class SSDP_notify(BaseTestCase):
    """SSDP notify test cases.

    These tests use the fact that multicast datagrams sent to the loopback
    interface are looped back to the notify task.
    """

    def test_ssdp_notify(self):
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback([SSDP_ALIVE], wait_for_termination=True))

        self.assertTrue(find_in_logs(m_logs.output, 'upnp', SSDP_ALIVE))

    def test_membership_OSError(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            try:
                notify = Notify(None, set())
                notify.manage_membership(set(['256.0.0.0']))
            finally:
                if notify is not None:
                    notify.sock.close()

        self.assertTrue(search_in_logs(m_logs.output, 'network',
            re.compile('256\.0\.0\.0 cannot be member of 239.255.255.250')))

    def test_notify_OSError(self):
        async def setup(control_point):
            patcher = mock.patch.object(control_point, '_process_ssdp')
            proc_ssdp = patcher.start()
            proc_ssdp.side_effect = OSError(err_msg)

        async def run_notify():
            control_point = await loopback([SSDP_ALIVE], setup=setup)
            # Wait until completion of the notify task.
            try:
                await asyncio.wait_for(control_point._notify_task, 1)
            except asyncio.TimeoutError:
                self.fail('Notify task did not terminate as expected')

        err_msg = 'Exception raised by _process_ssdp'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(run_notify())

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                       re.compile('on_con_lost.*done')))
        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                       re.compile(f'OSError\({err_msg!r}\)')))

    def test_invalid_field(self):
        field = 'invalid NTS field'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(loopback([SSDP_NOTIFY.format(nts=field),
                                  SSDP_ALIVE],
                                 wait_for_termination=True))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                        re.compile(f'malformed HTTP header:\n.*{field}',
                                           re.MULTILINE)))

    def test_no_NTS_field(self):
        not_nts = 'FOO: dummy field name'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(loopback([SSDP_NOTIFY.format(nts=not_nts),
                                  SSDP_ALIVE],
                                 wait_for_termination=True))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                        re.compile(f'missing "NTS" field')))

@requires_resources('os.devnull')
class SSDP_msearch(BaseTestCase):
    """SSDP msearch test cases."""

    @staticmethod
    def _sendto_coro(datagram):
        """Return a coroutine to send a datagram using a socket.

        The datagram is received by the MsearchServerProtocol instance.
        """

        async def _get_result(protocol):
            return protocol.get_result()

        async def send_datagram(ip, protocol):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.sendto(datagram.encode(),
                            (HOST, MSEARCH_PORT))
                try:
                    return await asyncio.wait_for(_get_result(protocol), 1)
                except asyncio.TimeoutError:
                    raise AssertionError ('The sent datagram has not been'
                                          ' received')
        return send_datagram

    def test_ssdp_msearch(self):
        async def _msearch(ip, protocol):
            await msearch(ip, protocol, msearch_count=1, msearch_interval=0,
                          mx=0)

        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(loopback(_msearch))

        self.assertTrue(search_in_logs(m_logs.output, 'network', re.compile(
            "Sent 1 M-SEARCH datagrams to \('239\.255\.255\.250', 1900\)")))

    def test_ssdp_socket_msearch(self):
        coro = self._sendto_coro(MSEARCH_RESPONSE)
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(coro, wait_for_termination=True))

        self.assertTrue(find_in_logs(m_logs.output, 'upnp', MSEARCH_RESPONSE))

    def test_bad_start_line(self):
        coro = self._sendto_coro(NOT_FOUND)
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(coro))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                re.compile(f"Ignore '{NOT_FOUND.splitlines()[0]}' request")))

    def test_not_root_device(self):
        device = 'urn:schemas-upnp-org:device:MediaServer:1'
        st = f'ST: {device}'
        coro = self._sendto_coro(SSDP_MSEARCH.format(st=st))
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(coro))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                re.compile(f"Ignore '{device}': non root device")))

    def test_invalid_ip(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(send_mcast('256.0.0.0', None))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                re.compile('Cannot bind.*256\.0\.0\.0')))

@requires_resources('os.devnull')
class SSDP_http(BaseTestCase):
    """Http test cases."""

    @staticmethod
    async def _loopback_get(body, content_length=None, start_line=None):
        """Start the http server and send the query."""

        http_server = HTTPServer(body, content_length, start_line)
        asyncio.create_task(http_server.run())
        await http_server.startup
        return await asyncio.wait_for(http_get(URL), 1)

    @staticmethod
    async def _loopback_soap(body, start_line=None):
        """Start the http server and send the query."""

        soap_body = 'The soap action'
        soap_header = f'Content-length: {len(body.encode())}\r\n'
        http_server = HTTPServer(body, start_line=start_line)
        asyncio.create_task(http_server.run())
        await http_server.startup
        return await asyncio.wait_for(http_soap(URL, soap_header, soap_body),
                                      1)

    def test_http_get(self):
        body = 'Some content.'
        received_body = asyncio.run(self._loopback_get(body))

        self.assertEqual(body, received_body.decode())

    def test_zero_length(self):
        body = 'Some content.'
        received_body = asyncio.run(self._loopback_get(body,
                                                       content_length=0))

        self.assertEqual(received_body, b'')

    def test_length_mismatch(self):
        body = 'Some content.'
        with self.assertRaises(UPnPInvalidHttpError) as cm:
            received_body = asyncio.run(self._loopback_get(body,
                                                           content_length=1))

        self.assertIn(f'mismatch (1 != {len(body)})', cm.exception.args[0])

    def test_bad_http_version(self):
        body = 'Some content.'
        start_line = 'HTTP/2.0 200 OK'
        with self.assertRaises(UPnPInvalidHttpError) as cm:
            received_body = asyncio.run(self._loopback_get(body,
                                                    start_line=start_line))

        self.assertIn(start_line, cm.exception.args[0])

    def test_http_soap(self):
        body = 'soap response'
        is_fault, received_body = asyncio.run(self._loopback_soap(body))

        self.assertEqual(body, received_body.decode())
        self.assertFalse(is_fault)

    def test_soap_fault(self):
        body = 'soap response'
        is_fault, received_body = asyncio.run(self._loopback_soap(body,
                            start_line='HTTP/1.0 500 Internal Server Error'))

        self.assertEqual(body, received_body.decode())
        self.assertTrue(is_fault)

    def test_bad_soap(self):
        body = 'soap response'
        start_line = 'HTTP/2.0 200 OK'
        with self.assertRaises(UPnPInvalidHttpError) as cm:
            is_fault, received_body = asyncio.run(self._loopback_soap(body,
                                                    start_line=start_line))

        self.assertIn(start_line, cm.exception.args[0])

if __name__ == '__main__':
    unittest.main(verbosity=2)
