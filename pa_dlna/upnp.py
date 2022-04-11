"""A basic UPnP Control Point asyncio library.

See "UPnP Device Architecture 2.0".

A client example:

    import asyncio
    from pa_dlna.upnp import UPnPControlPoint

    async def main(ipaddr_list):
        with UPnPControlPoint(ipaddr_list) as upnp:
            while True:
                notification, root_device = await upnp.get_notification()
                ...

'notification' is one of the 'alive' or 'byebye' strings.
'root_device' is an instance of UPnPDevice.

Use the UPnPDevice instance methods of root_device to learn about its embedded
devices and its services, and control each UPnP device with its service
instances to send soap requests to the corresponding UPnP device.
"""

import asyncio
import logging
import socket
import struct
import time
import collections
import re
import urllib.parse
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

# UPnP exceptions.
class UPnPError(Exception): pass
class UPnPFatalError(UPnPError): pass
class UPnPControlPointFatalError(UPnPFatalError): pass

# Temporary error UPnP exceptions.
class UPnPControlPointError(UPnPError): pass
class InvalidSsdpError(UPnPError): pass
class InvalidHttpError(UPnPError): pass

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
        raise InvalidSsdpError(f'malformed HTTP header:\n{header}')

def check_ssdp_header(header, is_msearch):
    """Check the SSDP header."""

    def exist(keys):
        for key in keys:
            if key not in header:
                raise InvalidSsdpError(
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
    except InvalidSsdpError as e:
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

    # Prevent multicast datagrams to be looped back to ourself.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

    # Let the operating system choose the port.
    sock.bind((ip, 0))

    # Start the server.
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: MsearchServerProtocol(ip), sock=sock)

    # Prepare the socket for sending from the network interface of 'ip'.
    sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
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

async def notify(ip_addresses, process_datagram):
    """Implement the SSDP advertisement protocol."""

    # See section 21.10 Sending and Receiving in
    # "Network Programming Volume 1, Third Edition" Stevens et al.
    # See also section 5.10.2 Receiving IP Multicast Datagrams
    # in "An Advanced 4.4BSD Interprocess Communication Tutorial".

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    for ip in ip_addresses:
        # Become a member of the IP multicast group on this interface.
        mreq = struct.pack('4s4s', socket.inet_aton(MCAST_GROUP),
                           socket.inet_aton(ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

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

async def http_get(url, host=None, port=80):
    """Return the body of the response to an HTTP 1.0 GET.

    RFC 1945: Hypertext Transfer Protocol -- HTTP/1.0
    """

    writer = None
    try:
        url = urllib.parse.urlsplit(url)
        host = url.hostname if url.hostname else host
        port = url.port if url.port else port
        reader, writer = await asyncio.open_connection(host, port)

        # Send the request.
        query = (
            f"GET {url.path or '/'} HTTP/1.0\r\n"
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
                        re.match('HTTP/1\.(0|1) 200 ', line) is None):
                    raise InvalidHttpError(f'Got "{line}" from {host}')
                header.append(line)
            else:
                break

        if not header:
            raise InvalidHttpError(f'Empty http header from {host}')
        header_dict = http_header_as_dict(header[1:])

        body = await reader.read()

        # Check that we have received the whole body.
        content_length = header_dict.get('CONTENT-LENGTH', None)
        if content_length is not None:
            content_length = int(content_length)
            if len(body) != content_length:
                raise InvalidHttpError(f'Content-Length and actual length'
                                f' mismatch ({content_length}-{len(body)})'
                                f' from {host}')
        return body

    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()

# Miscellaneous utilities.
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

# Network utilities.
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

# The components of an UPnP root device.
class UPnPElement:
    """An UPnP device or service."""

    def __init__(self, root_device, name):
        self.root_device = root_device
        self.name = name
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            if self.root_device is not None:
                self.root_device.close()

    def __str__(self):
        return self.name

class UPnPService(UPnPElement):
    """An UPnP service."""

    def __init__(self, root_device, name):
        super().__init__(root_device, name)
        self.enabled = True
        self.aio_tasks = root_device.aio_tasks

    def close(self):
        super().close()
        self.enabled = False

    def set_enable(self, state=True):
        # A service does not accept soap requests when its 'enabled' attribute
        # is False.
        self.enabled = state

class UPnPDevice(UPnPElement):
    """An UPnP device."""

    def __init__(self, root_device, name):
        super().__init__(root_device, name)
        self.services = set()           # list of services
        self.devices = set()            # list of embedded devices

    def close(self):
        super().close()
        for service in self.services:
            self.services.remove(service)
            service.close()
        for device in self.devices:
            self.devices.remove(device)
            device.close()

    def set_enable(self, state=True):
        for service in self.services:
            service.set_enable(state)
        for device in self.devices:
            device.set_enable(state)

    async def run(self, descr_xml):
        try:
            # XXX parse descr_xml
            # create and start embedded devices
            # create and start services
            pass
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            if not isinstance(self, UPnPRootDevice):
                logger.debug(f'End of {self} task')

class UPnPRootDevice(UPnPDevice):
    """An UPnP root device."""

    def __init__(self, control_point, udn, location, max_age):
        super().__init__(None, 'UPnPRootDevice')
        self.control_point = control_point  # UPnPControlPoint instance
        self.udn = udn
        self.location = location
        self.set_valid_until(max_age)
        self.enabled = True
        self.aio_tasks = AsyncioTasks()

    def close(self):
        super().close()
        self.set_enable(False)
        self.aio_tasks.cancel_all()

    def set_enable(self, state=True):
        # Used by the aging process to enable/disable all services and
        # embedded devices.
        self.enabled = state
        super().set_enable(state)

    def set_valid_until(self, max_age):
        # The 'valid_until' attribute is the monotonic date when the root
        # device and its services and embedded devices become disabled.
        # 'valid_until' None means no aging is performed.
        if max_age is not None:
            self.valid_until = time.monotonic() + max_age
        else:
            self.valid_until = None

    def get_timeleft(self):
        if self.valid_until is not None:
            return self.valid_until - time.monotonic()
        return None

    async def age_root_device(self):
        # Age the root device using SSDP alive notifications.
        while True:
            timeleft = self.get_timeleft()
            if timeleft is not None and timeleft > 0:
                if not self.enabled:
                    self.set_enable(True)
                    logger.info(f'{self} is up')
                await asyncio.sleep(timeleft)
            else:
                # Missing 'CACHE-CONTROL' field in SSDP.
                if timeleft is None:
                    if not self.enabled:
                        self.set_enable(True)
                        logger.info(f'{self} is up')
                elif self.enabled:
                    assert timeleft <= 0
                    self.set_enable(False)
                    logger.info(f'{self} is down')

                # Wake up every second to check for a change in
                # valid_until.
                await asyncio.sleep(1)

    async def run(self):
        try:
            description = await http_get(self.location)
            description = description.decode()
            # XXX logger.info(f'{description}')

            # XXX await super().run(descr_xml)

            self.control_point._put_notification('alive', self)
            await self.age_root_device()

        except Exception as e:
            logger.exception(f'{e!r}')
            self.close()
        finally:
            logger.debug(f'End of {self} task')

    def __str__(self):
        """Return a short representation of udn."""
        return f'UPnPRootDevice {shorten(self.udn)}'

# The UPnP control point.
class UPnPControlPoint:
    """An UPnP control point."""

    def __init__(self, ip_addresses, ttl=2):
        """Constructor.

        'ip_addresses' list of the local IPv4 addresses of the network
            interfaces where DLNA devices may be discovered.
        'ttl' IP packets time to live.
        """

        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self.closed = False
        self._upnp_queue = UPnPQueue()
        self._devices = {}              # {udn: UPnPRootDevice}
        self.aio_tasks = AsyncioTasks()

    def open(self):
        """Start the UPnP Control Point."""

        # Set up signal handlers.
        loop = asyncio.get_running_loop()
        for s in (SIGINT, SIGTERM):
            loop.add_signal_handler(s, lambda s=s: self.sig_handler(s))

        # Start the msearch task.
        self.aio_tasks.create_task(self._ssdp_msearch(), name='ssdp msearch')

        # Start the notify task.
        self.aio_tasks.create_task(self._ssdp_notify(), name='ssdp notify')

    def close(self, exc=None):
        """Close the UPnP Control Point."""

        if not self.closed:
            self.closed = True

            # Raise the exception in the get_notification() method.
            if exc is not None:
                self._upnp_queue.throw(exc)

            for root_device in self._devices.values():
                root_device.close()
            self.aio_tasks.cancel_all()
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

    def sig_handler(self, signal):
        errmsg = f'Got signal {strsignal(signal)}'
        logger.info(errmsg)
        if signal == SIGINT:
            self.close(exc=KeyboardInterrupt())
        else:
            self.close(exc=SystemExit())

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
            root_device = UPnPRootDevice(self, udn, header['LOCATION'],
                                         max_age)
            self.aio_tasks.create_task(root_device.run(),
                                       name=str(root_device))
            self._devices[udn] = root_device
            logger.info(f'New {root_device} at {ip_source}')

        else:
            root_device = self._devices[udn]

            # Avoid cluttering the logs when the aging refresh occurs within 5
            # seconds of the last one, assuming all max ages are the same.
            timeleft = root_device.get_timeleft()
            if (timeleft is not None and
                    max_age is not None and
                    max_age - timeleft > 5):
                logger.debug(f'Refresh with max-age={max_age}'
                             f' for {root_device}')

            # Refresh the aging time.
            root_device.set_valid_until(max_age)

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

        await notify(self.ip_addresses, self._process_ssdp)
        errmsg = 'Unexpected end of the notify coroutine'
        logger.error(errmsg)
        self.close(exc=UPnPControlPointFatalError(errmsg))

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
