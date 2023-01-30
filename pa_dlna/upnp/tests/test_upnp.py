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
from .device_resps import device_description, scpd, soap_response, soap_fault
from ..util import HTTPRequestHandler, shorten
from ..upnp import (UPnPControlPoint, UPnPRootDevice, UPnPService,
                    UPnPSoapFaultError, UPnPClosedDeviceError)
from ..xml import UPnPXMLError

SSDP_BYEBYE = SSDP_NOTIFY.format(nts='NTS: ssdp:byebye', **SSDP_PARAMS)
SSDP_UPDATE = SSDP_NOTIFY.format(nts='NTS: ssdp:update', **SSDP_PARAMS)
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

class HTTPServer:
    def __init__(self, soap_response, icons, devices):
        self.soap_response = soap_response
        self.icons = icons
        self.devices = devices
        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    def get_response(self, uri_path):
        header = ['HTTP/1.1 200 OK']
        if uri_path == '/MediaRenderer/desc.xml':
            body = device_description(icons=self.icons, devices=self.devices)
        else:
            for service in ('AVTransport', 'RenderingControl',
                            'ConnectionManager'):
                if uri_path == f'/{service}/desc.xml':
                    body = scpd()
                    break
                elif uri_path == f'/{service}/ctrl':
                    body = self.soap_response
                    if 'Fault>' in body:
                        header = ['HTTP/1.1 500 Internal Server Error']
                    break
            else:
                raise AssertionError(f'Unknown uri_path: {uri_path}')

        self.body = body.encode()
        header.extend([('Content-Length: ' + str(len(self.body))), '', ''])
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

async def start_http_server(soap_response=None, icons='', devices=''):
    http_server = HTTPServer(soap_response, icons, devices)
    asyncio.create_task(http_server.run())
    await http_server.startup

class ControlPoint(IsolatedAsyncioTestCase):
    """Control Point test cases."""

    @staticmethod
    async def _run_until_patch(datagrams, setup=None,
                               patch_method='_put_notification'):
        await start_http_server()
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
        finally:
            control_point.close()

class RootDevice(IsolatedAsyncioTestCase):
    """Root device test cases."""

    def setUp(self):
        self.control_point = UPnPControlPoint(['lo'], 3600)
        self.root_device = UPnPRootDevice(self.control_point, UDN, HOST, HOST,
                                          URL, 1800)

    async def test_OSError(self):
        exc = OSError('FOO')
        with self.assertLogs(level=logging.DEBUG) as m_logs,\
                mock.patch.object(self.root_device,
                                  '_parse_description') as parse:
            parse.side_effect = exc
            await start_http_server()
            await self.root_device._run()

        self.assertTrue(find_in_logs(m_logs.output, 'upnp',
                                     f'UPnPRootDevice._run(): {exc!r}'))

    async def test_missing_device(self):
        with mock.patch('pa_dlna.upnp.upnp.xml_of_subelement') as subelement,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            subelement.side_effect = [None]
            await start_http_server()
            await self.root_device._run()

        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
                        re.compile(f" Missing 'device' subelement in root"
                                   ' device description')))

    async def test_age_device(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await start_http_server()
            self.root_device._set_valid_until(0)
            await self.root_device._run()

        self.assertTrue(find_in_logs(m_logs.output, 'upnp',
                        f'Aging expired on UPnPRootDevice {shorten(UDN)}'))

    async def test_soap_action(self):
        response = soap_response(
            f"""
            <u:GetProtocolInfoResponse
                    xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">
                <Source></Source>
                <Sink></Sink>
            </u:GetProtocolInfoResponse>
        """)

        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(response)
            await self.root_device._run()

            service = self.root_device.serviceList[CONNECTIONMANAGER]
            self.assertTrue(isinstance(service, UPnPService))
            response = await service.soap_action('GetProtocolInfo', {},
                                                 log_debug=True)

        self.assertEqual(response, {'Source': None, 'Sink': None})

    async def test_soap_closed(self):
        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertRaises(UPnPClosedDeviceError) as cm,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server()
            await self.root_device._run()

            self.root_device.close()
            service = self.root_device.serviceList[CONNECTIONMANAGER]
            response = await service.soap_action('GetProtocolInfo', {})

    async def test_soap_fault(self):
        response = soap_fault()
        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertRaises(UPnPSoapFaultError) as cm,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(response)
            await self.root_device._run()

            service = self.root_device.serviceList[CONNECTIONMANAGER]
            response = await service.soap_action('GetProtocolInfo', {},
                                                 log_debug=True)

        self.assertEqual(cm.exception.args[0]._asdict(),
                {'errorCode': '401', 'errorDescription': 'Invalid Action'})

    async def test_icons(self):
        icons = """<iconList>
                     <icon>
                       <mimetype>image/jpeg</mimetype>
                       <width>48</width>
                       <height>48</height>
                       <depth>24</depth>
                       <url>/Icons/48x48.jpg</url>
                     </icon>
                   </iconList>"""

        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(icons=icons)
            await self.root_device._run()

        self.assertEqual(self.root_device.iconList[0]._asdict(),
                         {'mimetype': 'image/jpeg',
                          'width': '48',
                          'height': '48',
                          'depth': '24'
                          , 'url': '/Icons/48x48.jpg'})

    async def test_icons_namespace(self):
        icons = """<iconList>
                     <yamaha:icon>
                       <mimetype>image/jpeg</mimetype>
                       <width>48</width>
                       <height>48</height>
                       <depth>24</depth>
                       <url>/Icons/48x48.jpg</url>
                     </yamaha:icon>
                   </iconList>"""

        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(icons=icons)
            await self.root_device._run()

        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
            re.compile("UPnPXMLError: Found "
                "'{urn:schemas-yamaha-com:device-1-0}icon' instead of"
                " '{urn:schemas-upnp-org:device-1-0}icon'")))

    async def test_icons_missing(self):
        icons = """<iconList>
                     <icon>
                       <width>48</width>
                       <height>48</height>
                       <depth>24</depth>
                       <url>/Icons/48x48.jpg</url>
                     </icon>
                   </iconList>"""

        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(icons=icons)
            await self.root_device._run()

        self.assertTrue(search_in_logs(m_logs.output, 'upnp',
                    re.compile("Missing required subelement of 'icon' in"
                               " device description")))

    async def test_devices(self):
        device_type = 'urn:schemas-upnp-org:device:MediaRenderer:1'
        device_name = 'Embedded device name'
        devices = f"""<deviceList>
                        <device>
                          <deviceType>{device_type}</deviceType>
                          <friendlyName>{device_name}</friendlyName>
                        </device>
                      </deviceList>"""

        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(devices=devices)
            await self.root_device._run()

        embedded_devices = self.root_device.deviceList
        first_device = list(embedded_devices)[0]
        self.assertEqual(embedded_devices[first_device].friendlyName,
                         device_name)

    def tearDown(self):
        self.control_point.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
