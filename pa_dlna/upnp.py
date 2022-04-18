"""A basic UPnP Control Point asyncio library.

See "UPnP Device Architecture 2.0".

Example of using the Control Point (there is no external dependency):

>>> import asyncio
>>> from pa_dlna.upnp import UPnPControlPoint
>>>
>>> async def main(ipaddr_list):
...   with UPnPControlPoint(ipaddr_list) as control_point:
...     notification, root_device = await control_point.get_notification()
...     print(f"  Got '{notification}' from {root_device.ip_source}")
...     print(f'  deviceType: {root_device.deviceType}')
...     print(f'  friendlyName: {root_device.friendlyName}')
...     for service in root_device.services.values():
...       print(f'    serviceType: {service.serviceType}')
...
>>>
>>> asyncio.run(main(['192.168.0.254', '192.168.43.83']))
  Got 'alive' from 192.168.0.212
  deviceType: urn:schemas-upnp-org:device:MediaRenderer:1
  friendlyName: Yamaha RN402D
    serviceType: urn:schemas-upnp-org:service:AVTransport:1
    serviceType: urn:schemas-upnp-org:service:RenderingControl:1
    serviceType: urn:schemas-upnp-org:service:ConnectionManager:1
>>>

'notification' is one of the 'alive' or 'byebye' strings.
'root_device' is an instance of UPnPDevice.
'service' is an instance of UPnPService.

Use the UPnPDevice instance to learn about the embedded devices, services
and attributes of 'root_device'. Use the UPnPService instances methods
to manage control and eventing on the device. Set up the Python 'logging'
package to trace the library.
"""

import asyncio
import logging
import socket
import struct
import time
import collections
import re
import urllib.parse
import io
import sys
import xml.etree.ElementTree as ET
from signal import SIGINT, SIGTERM, strsignal

logger = logging.getLogger('upnp')

MCAST_GROUP = '239.255.255.250'
MCAST_PORT = 1900
MCAST_ADDR = (MCAST_GROUP, MCAST_PORT)
UPNP_ROOTDEVICE = 'upnp:rootdevice'

MSEARCH_COUNT= 3                        # number of MSEARCH requests each time
MSEARCH_EVERY = 60                      # sent every seconds
MSEARCH_INTERVAL = 0.2                  # sent at seconds intervals
MX = 2                                  # seconds to delay response

MSEARCH = '\r\n'.join([
        f'M-SEARCH * HTTP/1.1',
        f'HOST: {MCAST_GROUP}:{MCAST_PORT}',
        f'MAN: "ssdp:discover"',
        f'ST: {UPNP_ROOTDEVICE}',
        f'MX: {MX}',
    ]) + '\r\n'

UPNP_NAMESPACE_BEG = 'urn:schemas-upnp-org:'

# UPnP exceptions.
class UPnPError(Exception): pass

# Fatal error exceptions.
class UPnPFatalError(UPnPError): pass
class UPnPControlPointFatalError(UPnPFatalError): pass
class UPnPXMLFatalError(UPnPFatalError): pass

# Temporary error exceptions.
class UPnPControlPointError(UPnPError): pass
class UPnPInvalidSsdpError(UPnPError): pass
class UPnPInvalidHttpError(UPnPError): pass
class UPnPClosedDevice(UPnPError): pass

# Networking helper functions.
def shorten(txt, head_len=10, tail_len=5):
    if len(txt) <= head_len + 3 + tail_len:
        return txt
    return txt[:head_len] + '...' + txt[len(txt)-tail_len:]

def http_header_as_dict(header):
    """Return the http header as a dict."""

    def normalize(args):
        """Return a normalized (key, value) tuple."""
        return args[0].strip().upper(), args[1].strip()

    # RFC 2616 section 4.2: Header fields can be extended over multiple
    # lines by preceding each extra line with at least one SP or HT.
    compacted = ''
    for line in header:
        sep = '' if not compacted or line.startswith((' ', '\t')) else '\n'
        compacted = sep.join((compacted, line))

    try:
        return dict(normalize(line.split(':', maxsplit=1))
                    for line in compacted.splitlines())
    except ValueError:
        raise UPnPInvalidSsdpError(f'malformed HTTP header:\n{header}')

def check_ssdp_header(header, is_msearch):
    """Check the SSDP header."""

    def exist(keys):
        for key in keys:
            if key not in header:
                raise UPnPInvalidSsdpError(
                    f'missing "{key}" field in SSDP notify:\n{header}')

    # Check the presence of some required keys.
    if is_msearch:
        exist(('ST', 'LOCATION', 'USN'))
    else:
        exist(('NT', 'NTS', 'USN'))
        if header['NTS'] in ('ssdp:alive', 'ssdp:update'):
            exist(('LOCATION',))

def parse_ssdp(datagram, ip_source, is_msearch):
    """Return None when ignoring the SSDP, otherwise return a dict."""

    req_line = 'HTTP/1.1 200 OK' if is_msearch else 'NOTIFY * HTTP/1.1'

    # Ignore non 'notify' and non 'msearch' SSDPs.
    try:
       header = datagram.decode().splitlines()
    except UnicodeError:
        return None
    if not header:
        return None
    start_line = header[0].strip()
    if start_line != req_line:
        logger.debug(f"Ignore '{start_line}' request" f' from {ip_source}')
        return None

    # Parse the HTTP header as a dict.
    try:
        header = http_header_as_dict(header[1:])
        check_ssdp_header(header, is_msearch)
    except UPnPInvalidSsdpError as e:
        logger.warning(f'Error from {ip_source}: {e}')
        return None

    # Ignore non root device responses.
    _type = header['ST'] if is_msearch else header['NT']
    if _type != UPNP_ROOTDEVICE:
        return None

    return header

async def msearch(ip, ttl):
    """Implement the SSDP search protocol on the 'ip' network interface.

    Return the list of received (addr, datagram).
    """

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        # Prevent multicast datagrams to be looped back to ourself.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

        # Let the operating system choose the port.
        try:
            sock.bind((ip, 0))
        except OSError as e:
            raise OSError(e.args[0], f'{ip}: {e.args[1]}') from None

        # Start the server.
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: MsearchServerProtocol(ip), sock=sock)

        # Prepare the socket for sending from the network interface of 'ip'.
        sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

        expire = time.monotonic() + MX
        try:
            for i in range(MSEARCH_COUNT):
                await asyncio.sleep(MSEARCH_INTERVAL)
                if not protocol.closed():
                    protocol.m_search(MSEARCH, sock)

            if not protocol.closed():
                remain = expire - time.monotonic()
                if remain > 0:
                    await asyncio.sleep(expire - time.monotonic())
        finally:
            transport.close()
            return  protocol.get_result()
    finally:
        # Needed when OSError is raised upon binding the socket.
        sock.close()

async def notify(ip_addresses, process_datagram):
    """Implement the SSDP advertisement protocol."""

    # See section 21.10 Sending and Receiving in
    # "Network Programming Volume 1, Third Edition" Stevens et al.
    # See also section 5.10.2 Receiving IP Multicast Datagrams
    # in "An Advanced 4.4BSD Interprocess Communication Tutorial".

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        for ip in ip_addresses:
            # Become a member of the IP multicast group on this interface.
            mreq = struct.pack('4s4s', socket.inet_aton(MCAST_GROUP),
                               socket.inet_aton(ip))
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                mreq)
            except OSError as e:
                raise OSError(e.args[0], f'{ip}: {e.args[1]}') from None

        # Allow other processes to bind to the same multicast group and port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind to the multicast (group, port).
        # Binding to (INADDR_ANY, port) would also work, except
        # that in that case the socket would also receive the datagrams
        # destined to (any other address, MCAST_PORT).
        sock.bind(MCAST_ADDR)

        # Start the server.
        loop = asyncio.get_running_loop()
        on_con_lost = loop.create_future()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: NotifyServerProtocol(process_datagram, on_con_lost),
            sock=sock)

        try:
            await on_con_lost
        finally:
            transport.close()
    finally:
        # Needed when OSError is raised upon setting IP_ADD_MEMBERSHIP.
        sock.close()

async def http_get(url):
    """Return the body of the response to an HTTP 1.0 GET.

    RFC 1945: Hypertext Transfer Protocol -- HTTP/1.0
    """

    writer = None
    try:
        url = urllib.parse.urlsplit(url)
        host = url.hostname
        port = url.port if url.port is not None else 80
        reader, writer = await asyncio.open_connection(host, port)

        # Send the request.
        request = url._replace(scheme='')._replace(netloc='').geturl()
        query = (
            f"GET {request or '/'} HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            f"\r\n"
        )
        writer.write(query.encode('latin-1'))

        # Parse the http header.
        header = []
        while True:
            line = await reader.readline()
            if not line:
                break

            line = line.decode('latin1').rstrip()
            if line:
                if (not header and
                        re.match(r'HTTP/1\.(0|1) 200 ', line) is None):
                    raise UPnPInvalidHttpError(f'Got "{line}" from {host}')
                header.append(line)
            else:
                break

        if not header:
            raise UPnPInvalidHttpError(f'Empty http header from {host}')
        header_dict = http_header_as_dict(header[1:])

        body = await reader.read()

        # Check that we have received the whole body.
        content_length = header_dict.get('CONTENT-LENGTH', None)
        if content_length is not None:
            content_length = int(content_length)
            if len(body) != content_length:
                raise UPnPInvalidHttpError(f'Content-Length and actual length'
                                f' mismatch ({content_length}-{len(body)})'
                                f' from {host}')
        return body

    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()

# XML helper functions.
def upnp_org_etree(xml):
    """Return the element tree and UPnP namespace from an xml string."""
    upnp_namespace = UPnPNamespace(xml, UPNP_NAMESPACE_BEG)
    return ET.fromstring(xml), upnp_namespace

def build_etree(element):
    """Build an element tree to a bytes sequence and return it as a string."""
    etree = ET.ElementTree(element)
    with io.BytesIO() as output:
        etree.write(output, encoding='utf-8', xml_declaration=True)
        return output.getvalue().decode()

def xml_of_subelement(xml, tag):
    """Return the first 'tag' subelement as an xml string."""

    # Find the 'tag' subelement.
    root, namespace = upnp_org_etree(xml)
    element = root.find(f'{namespace!r}{tag}')

    if element is None:
        return None
    return build_etree(element)

def findall_childless(etree, namespace):
    """Return the dictionary {tag: text} of all chidless subelements."""
    d = {}
    ns_len = len(f'{namespace!r}')
    for e in etree.findall(f'.{namespace!r}*'):
        if e.tag and len(list(e)) == 0:
            tag = e.tag[ns_len:]
            d[tag] = e.text
    return d

# Helper class(es).
class UPnPNamespace:
    """A namespace value."""

    def __init__(self, xml, value_beg):
        """Use the namespace value starting with 'value_beg'."""

        self.value = None
        ns = dict(elem for event, elem in ET.iterparse(
            io.StringIO(xml), events=['start-ns']))

        # No namespaces.
        if not ns:
            self.value = ''

        for v in ns.values():
            if v.startswith(value_beg):
                self.value = v
                break

        if self.value is None:
            raise UPnPXMLFatalError(f'No namespace starting with {value_beg}')

    def __repr__(self):
        return f'{{{self.value}}}' if self.value else ''

class AsyncioTasks:
    """Save references to tasks, to avoid tasks being garbage collected.

    See Python github PR 29163 and the corresponding Python issues.
    """

    def __init__(self):
        self._tasks = set()             # references to tasks

    def create_task(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.remove(t))
        return task

    def cancel_all(self):
        for task in self:
            task.cancel()

    def __iter__(self):
        for t in self._tasks:
            yield t

class UPnPQueue:
    """An interruptible asyncio queue.

    UPnPQueue.get() keeps raising the UPnPFatalError exception once an
    instance of this exception has been thrown, whatever the fill state
    of the UPnPQueue.
    Other kinds of exceptions will only be raised once, when the exception is
    thrown while there is a pending call to get().
    """

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.waiter = None
        self.exception = None
        self.buffer = collections.deque()

    def throw(self, exc):
        self.exception = exc
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_exception(exc)
        elif not isinstance(exc, UPnPFatalError):
            self.exception = None

    def put_nowait(self, item):
        self.buffer.append(item)
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_result(None)

    async def get(self):
        if self.exception is not None:
            assert isinstance(self.exception, UPnPFatalError)
            raise self.exception

        # Wait for the buffer to be filled.
        if not len(self.buffer):
            if self.waiter is not None:
                raise UPnPControlPointError('Cannot have two tasks'
                                ' simultaneously waiting on an UPnPQueue')
            self.waiter = self.loop.create_future()
            try:
                await self.waiter
            finally:
                self.waiter = None

        if self.exception is not None:
            exc = self.exception
            if not isinstance(exc, UPnPFatalError):
                self.exception = None
            raise exc
        else:
            return self.buffer.popleft()

# Network protocols.
class MsearchServerProtocol:
    """The MSEARCH asyncio server."""

    def __init__(self, ip):
        self.ip = ip
        self.transport = None
        self._result = []               # list of received (addr, datagram)
        self._closed = None

    def connection_made(self, transport):
        self.transport = transport
        self._closed = False

    def datagram_received(self, data, addr):
        self._result.append((data, addr))

    def error_received(self, exc):
        logger.warning(f'Error received on {self.ip} by'
                       f' MsearchServerProtocol: {exc}')
        self.transport.abort()

    def connection_lost(self, exc):
        if exc is not None:
            logger.warning(f'Connection lost on {self.ip} by'
                           f' MsearchServerProtocol: {exc!r}')
        self._closed = True

    def m_search(self, message, sock):
        try:
            logger.debug(f'Send mcast M-SEARCH msg to {MCAST_ADDR}'
                         f' on {self.ip} interface')
            self.transport.sendto(message.encode(), MCAST_ADDR)
        except Exception as e:
            self.error_received(e)

    def get_result(self):
        return self._result

    def closed(self):
        return self._closed

class NotifyServerProtocol:
    """The NOTIFY asyncio server."""

    def __init__(self ,process_datagram, on_con_lost):
        self.process_datagram = process_datagram
        self.on_con_lost = on_con_lost
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            self.process_datagram(data, addr[0], False)
        except Exception as exc:
            self.error_received(exc)

    def error_received(self, exc):
        logger.error(f'Error received by NotifyServerProtocol: {exc!r}')
        self.transport.abort()

    def connection_lost(self, exc):
        if not self.on_con_lost.done():
            self.on_con_lost.set_result(True)
        msg = f': {exc!r}' if exc is not None else ''
        logger.info(f'Connection lost by NotifyServerProtocol{msg}')

# Components of an UPnP root device.
class UPnPElement:
    """An UPnP device or service."""

    def __init__(self, root_device):
        self._root_device = root_device
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            if self._root_device is not None:
                self._root_device.close()

class UPnPService(UPnPElement):
    """An UPnP service."""

    def __init__(self, root_device, attributes):
        super().__init__(root_device)
        for k, v in attributes.items():
            setattr(self, k, v)
        urlbase = root_device.urlbase
        self.SCPDURL = urllib.parse.urljoin(urlbase, self.SCPDURL)
        self.controlURL = urllib.parse.urljoin(urlbase, self.controlURL)
        self.eventSubURL = urllib.parse.urljoin(urlbase, self.eventSubURL)
        self.description = None
        self._enabled = True
        self._aio_tasks = root_device._aio_tasks

    def close(self):
        if not self._closed:
            super().close()
            self._enabled = False

    async def _run(self):
        description = await http_get(self.SCPDURL)
        self.description = description.decode()

        return self

    def _set_enable(self, state=True):
        # A service does not accept soap requests when its '_enabled'
        # attribute is False.
        self._enabled = state

class UPnPDevice(UPnPElement):
    """An UPnP device."""

    def __init__(self, root_device):
        super().__init__(root_device)
        self.services = {}              # {serviceType: UPnPService instance}
        self.devices = {}               # {deviceType: UPnPDevice instance}

    def close(self):
        if not self._closed:
            super().close()
            for service in self.services.values():
                service.close()
            self.services = {}
            for device in self.devices.values():
                device.close()
            self.devices = {}

    async def _create_services(self, services, namespace, root_device):
        """Create each UPnPService instance with its attributes.

        And await until its xml description has been parsed and the soap task
        started. 'services' is an etree element.
        """

        if services is None:
            return

        for element in services:
            if element.tag != f'{namespace!r}service':
                raise UPnPXMLFatalError(f"Found '{element.tag}' instead"
                                        f" of '{namespace!r}service'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLFatalError("Empty 'service' element")
            if 'serviceType' not in d:
                raise UPnPXMLFatalError("Missing 'serviceType' element")

            self.services[d['serviceType']] = await (
                                    UPnPService(root_device, d)._run())

    async def _create_devices(self, devices, namespace, root_device):
        """Instantiate the embedded UPnPDevice(s)."""

        if devices is None:
            return

        for element in devices:
            if element.tag != f'{namespace!r}device':
                raise UPnPXMLFatalError(f"Found '{element.tag}' instead"
                                        f" of '{namespace!r}device'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLFatalError("Empty 'device' element")
            if 'deviceType' not in d:
                raise UPnPXMLFatalError("Missing 'deviceType' element")

            description = build_etree(element)
            self.devices[d['deviceType']] = await (
                  UPnPDevice(root_device)._parse_description(description))

    async def _parse_description(self, description):
        """Parse the xml 'description'.

        Recursively instantiate the tree of embedded devices and their
        services. When this method returns, each UPnPService instance has
        parsed its description and started a task to handle soap requests.
        """

        self.description = description
        device_etree, namespace = upnp_org_etree(description)

        # Add the childless elements of the device element as instance
        # attributes of the UPnPDevice instance.
        d = findall_childless(device_etree, namespace)
        self.__dict__.update(d)
        if not hasattr(self, 'deviceType'):
            raise UPnPXMLFatalError("Missing 'deviceType' element")
        logger.info(f'New device whose type is {self.deviceType}')

        root_device = self if self._root_device is None else self._root_device

        services = device_etree.find(f'{namespace!r}serviceList')
        await self._create_services(services, namespace, root_device)

        # Recursion here: _create_devices() calls _parse_description()
        devices = device_etree.find(f'{namespace!r}deviceList')
        await self._create_devices(devices, namespace, root_device)

        return self

    def _set_enable(self, state=True):
        for service in self.services.values():
            service._set_enable(state)
        for device in self.devices.values():
            device._set_enable(state)

class UPnPRootDevice(UPnPDevice):
    """An UPnP root device."""

    def __init__(self, control_point, udn, ip_source, location, max_age):
        super().__init__(None)
        self.control_point = control_point  # UPnPControlPoint instance
        self.udn = udn
        self.ip_source = ip_source
        self.location = location
        self._set_valid_until(max_age)
        self.urlbase = None
        self._enabled = True
        self._aio_tasks = AsyncioTasks()

    def close(self):
        if not self._closed:
            super().close()
            self._set_enable(False)
            self._aio_tasks.cancel_all()

            # close() may be called within an exception handler.
            errmsg = f'{self} is closed'
            if sys.exc_info()[0] is None:
                raise UPnPClosedDevice(errmsg)
            else:
                logger.warning(errmsg)

    def _set_enable(self, state=True):
        # Used by the aging process to enable/disable all services and
        # embedded devices.
        self._enabled = state
        super()._set_enable(state)

    def _set_valid_until(self, max_age):
        # The '_valid_until' attribute is the monotonic date when the root
        # device and its services and embedded devices become disabled.
        # '_valid_until' None means no aging is performed.
        if max_age is not None:
            self._valid_until = time.monotonic() + max_age
        else:
            self._valid_until = None

    def _get_timeleft(self):
        if self._valid_until is not None:
            return self._valid_until - time.monotonic()
        return None

    async def _age_root_device(self):
        # Age the root device using SSDP alive notifications.
        while True:
            timeleft = self._get_timeleft()
            if timeleft is not None and timeleft > 0:
                if not self._enabled:
                    self._set_enable(True)
                    logger.info(f'{self} is up')
                await asyncio.sleep(timeleft)
            else:
                # Missing 'CACHE-CONTROL' field in SSDP.
                if timeleft is None:
                    if not self._enabled:
                        self._set_enable(True)
                        logger.info(f'{self} is up')
                elif self._enabled:
                    assert timeleft <= 0
                    self._set_enable(False)
                    logger.info(f'{self} is down')

                # Wake up every second to check for a change in
                # _valid_until.
                await asyncio.sleep(1)

    async def _run(self):
        try:
            description = await http_get(self.location)
            description = description.decode()

            # Find the 'URLBase' subelement (UPnP version 1.1).
            root, namespace = upnp_org_etree(description)
            element = root.find(f'{namespace!r}URLBase')
            self.urlbase = (element.text if element is not None else
                            self.location)

            device_description = xml_of_subelement(description, 'device')
            if device_description is None:
                raise UPnPXMLFatalError("Missing 'device' subelement in root"
                                        ' device description')
            await self._parse_description(device_description)
            self.control_point._put_notification('alive', self)
            await self._age_root_device()
        except UPnPClosedDevice as e:
            logger.warning(f'{e!r}')
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close()
        finally:
            logger.debug(f'End of {self} task')

    def __str__(self):
        """Return a short representation of udn."""
        return f'UPnPRootDevice {shorten(self.udn)}'

# UPnP control point.
class UPnPControlPoint:
    """An UPnP control point."""

    def __init__(self, ip_addresses, ttl=2):
        """Constructor.

        'ip_addresses' list of the local IPv4 addresses of the network
            interfaces where DLNA devices may be discovered.
        'ttl' IP packets time to live.
        """

        if not ip_addresses:
            raise UPnPControlPointFatalError('The list of ip addresses cannot'
                                             ' be empty')
        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self._closed = False
        self._upnp_queue = UPnPQueue()
        self._devices = {}              # {udn: UPnPRootDevice}
        self._aio_tasks = AsyncioTasks()

    def open(self):
        """Start the UPnP Control Point."""

        # Set up signal handlers.
        loop = asyncio.get_running_loop()
        for s in (SIGINT, SIGTERM):
            loop.add_signal_handler(s, lambda s=s: self._sig_handler(s))

        # Start the msearch task.
        self._aio_tasks.create_task(self._ssdp_msearch(), name='ssdp msearch')

        # Start the notify task.
        self._aio_tasks.create_task(self._ssdp_notify(), name='ssdp notify')

    def close(self, exc=None):
        """Close the UPnP Control Point."""

        if not self._closed:
            self._closed = True

            # Raise the exception in the get_notification() method.
            if exc is not None:
                self._upnp_queue.throw(exc)

            for root_device in self._devices.values():
                try:
                    root_device.close()
                except UPnPClosedDevice as e:
                    logger.info(f'{e!r}')

            self._aio_tasks.cancel_all()
            logger.debug('End of upnp task')

    async def get_notification(self):
        """Return the tuple ('alive' or 'byebye', UPnPDevice instance).

        Raise unhandled exceptions occuring in the library, including
        KeyboardInterrupt and SystemExit.
        """

        return await self._upnp_queue.get()

    def _put_notification(self, kind, root_device):
        self._upnp_queue.put_nowait((kind, root_device))
        state = 'created' if kind == 'alive' else 'deleted'
        logger.info(f'{root_device} has been {state}')

    def _sig_handler(self, signal):
        errmsg = f'Got signal {strsignal(signal)}'
        logger.info(errmsg)
        if signal == SIGINT:
            self.close(exc=UPnPControlPointFatalError(
                                                f'{KeyboardInterrupt()!r}'))
        else:
            self.close(exc=UPnPControlPointFatalError(
                                                f'{SystemExit()!r}'))

    def _create_root_device(self, header, ip_source):

        # Get the max-age.
        # 'max_age' None means no aging.
        max_age = None
        cache = header.get('CACHE-CONTROL', None)
        if cache is not None:
            age = 'max-age='
            try:
                max_age = int(cache[cache.index(age)+len(age):])
            except ValueError:
                logger.warning(f'Invalid CACHE-CONTROL field in'
                               f' SSDP notify from {ip_source}:\n{header}')
                return

        udn = header['USN'].split('::')[0]
        if udn not in self._devices:
            # Instantiate the UPnPDevice and start its task.
            root_device = UPnPRootDevice(self, udn, ip_source,
                                         header['LOCATION'], max_age)
            self._aio_tasks.create_task(root_device._run(),
                                       name=str(root_device))
            self._devices[udn] = root_device
            logger.info(f'New {root_device} at {ip_source}')

        else:
            root_device = self._devices[udn]

            # Avoid cluttering the logs when the aging refresh occurs within 5
            # seconds of the last one, assuming all max ages are the same.
            timeleft = root_device._get_timeleft()
            if (timeleft is not None and
                    max_age is not None and
                    max_age - timeleft > 5):
                logger.debug(f'Refresh with max-age={max_age}'
                             f' for {root_device}')

            # Refresh the aging time.
            root_device._set_valid_until(max_age)

    def _process_ssdp(self, datagram, ip_source, is_msearch):
        """Process the received datagrams.

        'is_msearch' is True when processing a msearch response, otherwise it
        is a notify advertisement.
        """

        header = parse_ssdp(datagram, ip_source, is_msearch)
        if header is None:
            return

        msg = 'msearch response' if is_msearch else 'notify advertisement'
        logger.debug(f'Got {msg} from {ip_source}')

        if is_msearch or (header['NTS'] == 'ssdp:alive'):
            self._create_root_device(header, ip_source)
        else:
            nts = header['NTS']
            if nts == 'ssdp:byebye':
                udn = header['USN'].split('::')[0]
                root_device = self._devices.get(udn, None)
                if root_device is not None:
                    root_device.close()
                    self._put_notification('byebye', root_device)
                    del self._devices[udn]

            elif nts == 'ssdp:update':
                logger.warning(f'Ignore not supported {nts} notification'
                               f' from {ip_source}')

            else:
                logger.warning(f"Unknown NTS field '{nts}' in SSDP notify"
                               ' from {ip_source}')

    async def _ssdp_msearch(self):
        """Send msearch multicast SSDPs and process unicast responses."""

        try:
            while True:
                for ip in self.ip_addresses:
                    result = await msearch(ip, self.ttl)
                    for (datagram, src_addr) in result:
                        self._process_ssdp(datagram, src_addr[0],
                                           is_msearch=True)
                await asyncio.sleep(MSEARCH_EVERY)
        except Exception as e:
            exc = f'{e!r}'
            logger.exception(exc)
            self.close(exc=UPnPControlPointFatalError(exc))

    async def _ssdp_notify(self):
        """Listen to SSDP notifications."""

        try:
            await notify(self.ip_addresses, self._process_ssdp)
        except Exception as e:
            exc = f'{e!r}'
            logger.exception(exc)
            self.close(exc=UPnPControlPointFatalError(exc))

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
