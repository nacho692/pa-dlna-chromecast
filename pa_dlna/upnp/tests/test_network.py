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
MSEARCH_RESPONSE = '\r\n'.join([
    'HTTP/1.1 200 OK',
    'Location: http://192.168.0.212:49154/MediaRenderer/desc.xml',
    'Cache-Control: max-age=1800',
    'Content-Length: 0',
    'Server: Linux',
    'EXT:',
    'ST: upnp:rootdevice',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '',
    '',
])

async def loopback(datagrams, wait_for_termination=False, setup=None,
                   coro=None):
    """Loopback the 'datagrams' to the notify task.

    'setup' is a coroutine to be awaited for before sending the datagrams.
    'coro' is a coroutine to be used instead of upnp.network.msearch()
    """

    async def send_datagrams(ip, protocol):
        for datagram in datagrams:
            protocol.send_datagram(datagram)

    async def termination():
        while True:
            await asyncio.sleep(0)
            if create.called or remove.called:
                break

    if coro is None:
        coro = send_datagrams
    control_point = UPnPControlPoint(['lo'], 3600)
    try:
        with mock.patch.object(control_point,
                               '_create_root_device') as create,\
                mock.patch.object(control_point,
                               '_remove_root_device') as remove,\
                mock.patch.object(control_point,
                               '_ssdp_msearch') as search:

            # Prevent the msearch task to run UPnPControlPoint._ssdp_msearch.
            search.side_effect = [None]
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

if __name__ == '__main__':
    unittest.main(verbosity=2)
