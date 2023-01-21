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
from ..network import send_mcast

NTS_ALIVE = 'NTS: ssdp:alive'
SSDP_NOTIFY = '\r\n'.join([
    'NOTIFY * HTTP/1.1',
    'Host: 239.255.255.250:1900',
    'Content-Length: 0',
    'Location: http://127.0.0.1:49154/MediaRenderer/desc.xml',
    'Cache-Control: max-age=1800',
    'Server: Linux',
    'NT: upnp:rootdevice',
    '{nts}',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '',
    '',
])
SSDP_ALIVE = SSDP_NOTIFY.format(nts=NTS_ALIVE)

MSEARCH_PORT = 9999
ST_ROOT_DEVICE = 'ST: upnp:rootdevice'
SSDP_MSEARCH = '\r\n'.join([
    'HTTP/1.1 200 OK',
    'Location: http://192.168.0.212:49154/MediaRenderer/desc.xml',
    'Cache-Control: max-age=1800',
    'Content-Length: 0',
    'Server: Linux',
    'EXT:',
    '{st}',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '',
    '',
])
MSEARCH_RESPONSE = SSDP_MSEARCH.format(st=ST_ROOT_DEVICE)

NOT_FOUND_REASON = 'A dummy reason'
NOT_FOUND = '\r\n'.join([
    f'HTTP/1.1 404 {NOT_FOUND_REASON}',
    'Content-Length: 0',
    '',
    '',
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

async def get_result(protocol):
    """Loop for ever waiting for a result.

    'protocol' is the protocol of the MsearchServerProtocol instance.
    """

    while True:
        await asyncio.sleep(0)
        result = protocol.get_result()
        if result:
            return result

def sendto_coro(datagram):
    """Return a coroutine to send a datagram using directly a socket.

    The datagram is received by the MsearchServerProtocol instance.
    """

    async def send_datagram(ip, protocol):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(datagram.encode(),
                        ('127.0.0.1', MSEARCH_PORT))
            try:
                return await asyncio.wait_for(get_result(protocol), 1)
            except asyncio.TimeoutError:
                raise AssertionError ('The sent datagram has not been'
                                      ' received')
    return send_datagram

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

    def test_ssdp_msearch(self):
        coro = sendto_coro(MSEARCH_RESPONSE)
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(coro, wait_for_termination=True))

        self.assertTrue(find_in_logs(m_logs.output, 'upnp', MSEARCH_RESPONSE))

    def test_bad_start_line(self):
        coro = sendto_coro(NOT_FOUND)
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(coro))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                re.compile(f"Ignore '{NOT_FOUND.splitlines()[0]}' request")))

    def test_not_root_device(self):
        device = 'urn:schemas-upnp-org:device:MediaServer:1'
        st = f'ST: {device}'
        coro = sendto_coro(SSDP_MSEARCH.format(st=st))
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(coro))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                re.compile(f"Ignore '{device}': non root device")))
    def test_invalid_ip(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(send_mcast('256.0.0.0', None))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                re.compile('Cannot bind.*256\.0\.0\.0')))

if __name__ == '__main__':
    unittest.main(verbosity=2)
