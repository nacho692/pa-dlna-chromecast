"""UPnP test cases."""

import re
import asyncio
import logging
import urllib
from unittest import mock, IsolatedAsyncioTestCase

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import (loopback_datagrams, find_in_logs, search_in_logs, UDN, HOST,
               HTTP_PORT, SSDP_NOTIFY, SSDP_PARAMS, SSDP_ALIVE, URL,
               min_python_version)
from .device_resps import device_description, scpd
from ..util import HTTPRequestHandler, shorten
from ..upnp import UPnPControlPoint

SSDP_BYEBYE = SSDP_NOTIFY.format(nts='NTS: ssdp:byebye', **SSDP_PARAMS)
SSDP_UPDATE = SSDP_NOTIFY.format(nts='NTS: ssdp:update', **SSDP_PARAMS)

class HTTPServer:
    def __init__(self):
        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    def get_response(self, uri_path):
        if uri_path == '/MediaRenderer/desc.xml':
            body = device_description()
        else:
            for service in ('AVTransport', 'RenderingControl',
                            'ConnectionManager'):
                if uri_path == f'/{service}/desc.xml':
                    body = scpd()
                    break
            else:
                raise AssertionError(f'Unknown uri_path: {uri_path}')

        self.body = body.encode()
        header = ['HTTP/1.1 200 OK',
                  ('Content-Length: ' + str(len(self.body))),
                  '', ''
                  ]
        self.header = '\r\n'.join(header).encode('latin-1')

    async def client_connected(self, reader, writer):
        """Handle an HTTP GET request and return the response."""

        peername = writer.get_extra_info('peername')
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()
            uri_path = urllib.parse.unquote(handler.path)
            self.get_response(uri_path)

            # Write the response.
            writer.write(self.header)
            writer.write(self.body)
        finally:
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    async def run(self):
        aio_server = await asyncio.start_server(self.client_connected,
                                                HOST, HTTP_PORT)
        async with aio_server:
            self.startup.set_result(None)
            await aio_server.serve_forever()

class ControlPoint(IsolatedAsyncioTestCase):
    """Control Point test cases."""

    @staticmethod
    async def _run_until_patch(datagrams, setup=None,
                               patch_method='_put_notification'):
        http_server = HTTPServer()
        asyncio.create_task(http_server.run())
        await http_server.startup
        return await loopback_datagrams(datagrams, setup=setup,
                                        patch_method=patch_method)

    async def test_alive(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([SSDP_ALIVE])

        self.assertTrue(find_in_logs(m_logs.output, 'upnp',
                        'New UPnP services: AVTransport, RenderingControl,'
                        ' ConnectionManager'))
        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
                re.compile('UPnPRootDevice uuid:fffff.* has been created')))

    async def test_update(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([SSDP_UPDATE, SSDP_ALIVE])

        self.assertTrue(find_in_logs(m_logs.output, 'upnp',
                f'Ignore not supported ssdp:update notification from {HOST}'))

    async def test_bad_nts(self):
        nts_field = 'ssdp:FOO'
        nts = f'NTS: {nts_field}'
        ssdp_bad_nts = SSDP_NOTIFY.format(nts=nts, **SSDP_PARAMS)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([ssdp_bad_nts, SSDP_ALIVE])

        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
                                re.compile(f"Unknown NTS field '{nts_field}'")))

    async def test_byebye(self):
        async def setup(control_point):
            root_device = mock.MagicMock()
            root_device.__str__.side_effect = [device_name]
            control_point._devices[UDN] = root_device

        device_name = '_Some root device name_'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([SSDP_BYEBYE], setup=setup)

        self.assertTrue(find_in_logs(m_logs.output, 'upnp',
                                     f'{device_name} has been deleted'))

    async def test_faulty_device(self):
        async def setup(control_point):
            control_point._faulty_devices.add(udn)

        udn = 'uuid:ffffffff-ffff-ffff-ffff-000000000000'
        ssdp_params = { 'url': URL,
                        'max_age': '1800',
                        'nts': 'NTS: ssdp:alive',
                        'udn': udn
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([ssdp_alive, SSDP_ALIVE], setup=setup)

        self.assertTrue(find_in_logs(m_logs.output, 'upnp',
                                f'Ignore faulty root device {shorten(udn)}'))

    async def test_remove_device(self):
        class RootDevice:
            def __init__(self, udn): self.udn = udn
            def close(self): pass
            def __str__(self): return shorten(udn)

        async def setup(control_point):
            control_point._devices[udn] = root_device
            control_point._remove_root_device(udn, exc=OSError())

        udn = 'uuid:ffffffff-ffff-ffff-ffff-000000000000'
        root_device = RootDevice(udn)
        ssdp_params = { 'url': URL,
                        'max_age': '1800',
                        'nts': 'NTS: ssdp:alive',
                        'udn': udn
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            control_point = await self._run_until_patch(
                            [ssdp_alive, SSDP_ALIVE], setup=setup,
                            patch_method='_create_root_device')

        self.assertTrue(control_point.is_disabled(root_device))
        self.assertTrue(find_in_logs(m_logs.output,
            'upnp', f'Add {shorten(udn)} to the list of faulty root devices'))

    async def test_bad_max_age(self):
        max_age = 'FOO'
        ssdp_params = { 'url': URL,
                        'max_age': f'{max_age}',
                        'nts': 'NTS: ssdp:alive',
                        'udn': UDN
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([ssdp_alive, SSDP_ALIVE])

        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
            re.compile(f'Invalid CACHE-CONTROL field.*\n.*max-age={max_age}',
                       re.MULTILINE)))

    async def test_refresh(self):
        ssdp_params = { 'url': URL,
                        'nts': 'NTS: ssdp:alive',
                        'udn': UDN
                       }
        ssdp_alive_first = SSDP_NOTIFY.format(max_age=10, **ssdp_params)
        ssdp_alive_second = SSDP_NOTIFY.format(max_age=20, **ssdp_params)
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await self._run_until_patch([ssdp_alive_first, ssdp_alive_second])

        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
                            re.compile('Refresh with max-age=20')))

    @min_python_version((3, 9))
    async def test_close(self):
        async def close_with_exc(obj, exc):
            obj.close(exc=exc)

        exc = OSError('FOO')
        control_point = UPnPControlPoint(['lo'], 3600)
        try:
            await control_point.open()
            await asyncio.create_task(close_with_exc(control_point, exc))
        except asyncio.CancelledError as e:
            self.assertEqual(e.args[0], exc)
        else:
            raise AssertionError('Current task not cancelled')

if __name__ == '__main__':
    unittest.main(verbosity=2)
