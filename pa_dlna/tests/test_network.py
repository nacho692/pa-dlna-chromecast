"""Network test cases."""

import re
import asyncio
import logging
from unittest import TestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import find_in_logs, search_in_logs
from .. import TEST_LOGLEVEL
from ..upnp.upnp import UPnPControlPoint
from ..upnp.network import send_mcast

SSDP_ALIVE = '\r\n'.join([
    'NOTIFY * HTTP/1.1',
    'Host: 239.255.255.250:1900',
    'Content-Length: 0',
    'Location: http://127.0.0.1:49154/MediaRenderer/desc.xml',
    'Cache-Control: max-age=1800',
    'Server: Linux',
    'NT: upnp:rootdevice',
    'NTS: ssdp:alive',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '',
    '',
])

async def loopback(datagram, wait_for_termination=False, setup=None):
    """Loopback 'datagram' to the notify task.

    'setup' is a coroutine to be awaited for before sending the datagram.
    """

    async def send_datagram(ip, protocol):
        protocol.send_datagram(datagram)

    async def termination():
        while True:
            await asyncio.sleep(0)
            if create.called or remove.called:
                break

    semaphore = asyncio.Semaphore()
    async with UPnPControlPoint(['lo'], 3600,
                                semaphore=semaphore) as control_point:
        with mock.patch.object(control_point,
                               '_create_root_device') as create,\
                mock.patch.object(control_point,
                               '_remove_root_device') as remove:

            create.side_effect = control_point.close
            remove.side_effect = control_point.close
            if setup is not None:
                await setup(control_point)
            await send_mcast('127.0.0.1', semaphore, coro=send_datagram)
            if wait_for_termination:
                try:
                    await asyncio.wait_for(termination(), 1)
                except asyncio.TimeoutError:
                    raise AssertionError('_create_root_device() and '
                                '_remove_root_device() not called') from None

    return control_point

class SSDP(TestCase):
    """SSDP test cases.

    These tests use the fact that multicast datagrams sent to the loopback
    interface are looped back to the notify task.
    """

    def test_ssdp_alive(self):
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            asyncio.run(loopback(SSDP_ALIVE, wait_for_termination=True))

        self.assertTrue(find_in_logs(m_logs.output, 'upnp', SSDP_ALIVE))

    def test_sendmcast_error(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            asyncio.run(send_mcast('256.0.0.0', None))

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                re.compile('Cannot bind.*256\.0\.0\.0')))

    def test_notify_OSError(self):
        async def setup(control_point):
            patcher = mock.patch.object(control_point, '_process_ssdp')
            proc_ssdp = patcher.start()
            proc_ssdp.side_effect = OSError(err_msg)

        async def run_notify():
            control_point = await loopback(SSDP_ALIVE, setup=setup)
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

if __name__ == '__main__':
    unittest.main(verbosity=2)
