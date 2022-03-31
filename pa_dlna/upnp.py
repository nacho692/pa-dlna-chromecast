"""A simple UPnP Control Point asyncio library.

See UPnP Device Architecture 2.0.

A client example:

import asyncio
from pa_dlna.upnp import UPnPControlPoint

async def main(ipaddr_list, ttl, aging):
    with UPnPControlPoint(ipaddr_list, ttl, aging) as upnp:
        notification, root_device = await upnp.get_notification()
        ...

'notification' is one of the 'alive' or 'byebye' strings.
'root_device' is the root device, an instance of UPnPDevice.

Use the UPnPDevice instance methods of root_device to learn about its embedded
devices and its services, and control each UPnP device with its service
instances to send soap requests to the corresponding device.
"""

import asyncio
import logging
import socket
import struct
import hashlib
import time

logger = logging.getLogger('upnp')
mcast_group = '239.255.255.250'
mcast_port = 1900

def shorten(txt, head_len=10, tail_len=5):
    if len(txt) <= head_len + 3 + tail_len:
        return txt
    return txt[:head_len] + '...' + txt[len(txt)-tail_len:]

class InvalidSsdpError(Exception): pass

def http_header_as_dict(header):

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
                    for line in compacted.splitlines()[1:])
    except ValueError:
        raise InvalidSsdpError(f'malformed HTTP header:\n{header}')

def parse_notify_header(header):
    """Return the SSDP notify header as a dict."""

    def is_key(key):
        if key not in header_dict:
            raise InvalidSsdpError(
                f'missing "{key}" field in SSDP notify:\n{header}')

    header_dict = http_header_as_dict(header)

    # Check the presence of some required keys.
    for key in ('NT', 'NTS', 'USN'):
        is_key(key)
    if header_dict['NTS'] in ('ssdp:alive', 'ssdp:update'):
        is_key('LOCATION')
    return header_dict

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

class RcvFromSocket(socket.socket):
    """Implementation of socket.recvfrom() as a coroutine.

    The buffer size is passed in the constructor as 'bufsize'.
    """

    def __init__(self, *args, **kwargs):
        if 'bufsize' in kwargs:
            self.bufsize = kwargs['bufsize']
            del kwargs['bufsize']
        else:
            self.bufsize = 4096
        super().__init__(*args, **kwargs)
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.loop.add_reader(self.fileno(), self.reader)

    def reader(self):
        data, src_addr = super().recvfrom(self.bufsize)
        self.queue.put_nowait((data, src_addr))

    async def recvfrom(self):
        # The size of the buffer is set in the constructor.
        return await self.queue.get()

    def close(self):
        self.loop.remove_reader(self.fileno())
        super().close()

class UPnPElement:
    """An UPnP device or a service."""

    def __init__(self, root_device, name):
        self.root_device = root_device
        self.name = name
        self.closed = False
        self.aio_tasks = AsyncioTasks()

    def close(self):
        if not self.closed:
            self.closed = True
            if self.root_device is not None:
                self.root_device.close()
            self.aio_tasks.cancel_all()

    def __str__(self):
        return self.name

class UPnPService(UPnPElement):
    """An UPnP service."""

    def __init__(self, root_device, name):
        super().__init__(root_device, name)
        self.enabled = True

    def close(self):
        if not self.closed:
            super().close()
            self.enabled = False

    def set_enable(self, state=True):
        self.enabled = state

class UPnPDevice(UPnPElement):
    """An UPnP embedded device."""

    def __init__(self, root_device, name):
        super().__init__(root_device, name)
        self.services = set()           # list of services
        self.devices = set()            # list of embedded devices

    def close(self):
        """Close the UPnP device, its services and embedded devices."""

        if not self.closed:
            super().close()
            for service in self.services:
                self.services.remove(service)
                service.close()
            for device in self.devices:
                self.devices.remove(device)
                device.close()

    def set_enable(self, state=True):
        """Set the state of the services and embedded devices.

        A service does not accept soap requests when its 'enabled' attribute
        is False.
        """

        for service in self.services:
            service.set_enable(state)
        for device in self.devices:
            device.set_enable(state)

    async def run(self, descr_xml):
        try:
            # XXX parse descr_xml
            # start services
            # start embedded devices
            pass
        finally:
            if not isinstance(self, UPnPRootDevice):
                logger.debug(f'end of {self} task')

class UPnPRootDevice(UPnPDevice):
    """An UPnP root device."""

    def __init__(self, control_point, udn, location, max_age=None):
        super().__init__(None, 'UPnPRootDevice')
        self.control_point = control_point  # the UPnP instance
        self.udn = udn
        self.location = location
        self.enabled = True
        self.update(max_age)

    def close(self):
        if not self.closed:
            super().close()
            self.enabled = False

    def update(self, max_age):
        if max_age is not None:
            self.max_age = time.monotonic() + max_age
        else:
            self.max_age = None

    def set_enable(self, state=True):
        # Only the root device may trigger a change in the 'enabled' state
        # of its services and the services of the embedded devices.
        super().set_enable(state)
        self.enabled = state

    async def run(self):
        try:
            # XXX get and parse description
            # await super().run(descr_xml)
            self.control_point._put_notification('alive', self)

            # Aging is not implemented when 'max_age' is None.
            if self.max_age is None:
                return

            # Age the root device using SSDP alive notifications.
            while True:
                t = time.monotonic()
                if t <= self.max_age:
                    if not self.enabled:
                        self.set_enable()
                        logger.info(f'{self} is up')
                    await asyncio.sleep(self.max_age - t)
                else:
                    if self.enabled:
                        self.set_enable(False)
                        logger.info(f'{self} is down')

                    # Wake up every second to check for a change in max_age.
                    await asyncio.sleep(1)
        finally:
            logger.debug(f'end of {self} task')

    def __str__(self):
        """Return a short representation of udn."""
        return f'UPnPRootDevice {shorten(self.udn)}'

class UPnPControlPoint:
    """An UPnP control point."""

    def __init__(self, ip_addresses, ttl=2, aging=True):
        """Constructor.

        'ip_addresses' list of the local IPv4 addresses of the network
            interfaces where DLNA devices may be discovered.
        'ttl' IP packets time to live.
        """

        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self.aging = aging
        self.closed = False
        self._queue = asyncio.Queue()
        self._devices = {}              # {udn: UPnPRootDevice}
        self.aio_tasks = AsyncioTasks()
        self.invalid_ssdp_set = set()

    def open(self):
        """Start the UPnP Control Point."""

        # Start the msearch task.
        self.aio_tasks.create_task(self._ssdp_msearch(), name='ssdp msearch')
        # Start the notify task.
        self.aio_tasks.create_task(
            self._ssdp_notify(self.ip_addresses), name='ssdp notify')

    def close(self):
        """Close the UPnP Control Point."""

        if not self.closed:
            self.closed = True
            self.aio_tasks.cancel_all()
            logger.debug('end of upnp task')

    async def get_notification(self):
        """Return the tuple ('alive' or 'byebye', UPnPDevice instance)."""
        return await self._queue.get()

    def _put_notification(self, kind, root_device):
        self._queue.put_nowait((kind, root_device))
        state = 'created' if kind == 'alive' else 'deleted'
        logger.info(f'{root_device} has been {state}')

    def _handle_notify(self, datagram, ip_source):
        # Ignore non SSDP notify datagrams.
        try:
           header = datagram.decode().splitlines()
        except UnicodeError:
            return
        if not header or header[0].strip() != 'NOTIFY * HTTP/1.1':
            if header:
                logger.debug(f"ignore '{header[0].strip()}' start line"
                             f' from {ip_source}')
            return

        # Parse the HTTP header as a dict.
        try:
            header = parse_notify_header(header)
            nts = header['NTS']
            udn = header['USN'].split('::')[0]

            # 'max_age' None means no aging.
            max_age = None
            if nts == 'ssdp:alive' and self.aging:
                cache = header.get('CACHE-CONTROL', None)
                if cache is not None:
                    age = 'max-age='
                    try:
                        max_age = int(cache[cache.index(age)+len(age):])
                    except ValueError:
                        raise InvalidSsdpError(
                            f'invalid CACHE-CONTROL field in'
                            f' SSDP notify:\n{header}')
        except InvalidSsdpError as e:
            # Log invalid SSDP PDU only once.
            hash = hashlib.sha1(datagram).digest()
            if hash not in self.invalid_ssdp_set:
                self.invalid_ssdp_set.add(hash)
                logger.warning(f'{e}')
            return

        if nts == 'ssdp:alive':
            if udn not in self._devices:
                # Instantiate the UPnPDevice and start its task.
                root_device = UPnPRootDevice(self, udn, header['LOCATION'],
                                             max_age)
                self.aio_tasks.create_task(root_device.run(),
                                           name=str(root_device))
                self._devices[udn] = root_device
                logger.info(f'New {root_device}')
            else:
                root_device = self._devices[udn]
                root_device.update(max_age)
                logger.debug(f'refresh with max_age={max_age}'
                             f' for {root_device}')

        elif nts == 'ssdp:byebye':
            root_device = self._devices.get(udn, None)
            if root_device is not None:
                root_device.close()
                self._put_notification('byebye', root_device)
                del self._devices[udn]

        elif nts == 'ssdp:update':
            logger.warning(f'ignore not supported {nts} notification')

        else:
            logger.warning(f"unknown NTS field '{nts}' in SSDP notify")

    async def _ssdp_msearch(self):
        # XXX See https://tldp.org/HOWTO/Multicast-HOWTO-6.html.
        try:
            await asyncio.sleep(1)
        except Exception as e:
            logger.exception(e)
        finally:
            logger.debug('end of msearch task')

    async def _ssdp_notify(self, ip_addresses):
        """Listen on SSDP notifications."""

        # See section 21.10 Sending and Receiving in
        # "Network Programming Volume 1, Third Edition" Stevens et al.
        # See also section 5.10.2 Receiving IP Multicast Datagrams
        # in "An Advanced 4.4BSD Interprocess Communication Tutorial".

        sock_list = []
        sock = None
        try:
            # Become a member of the IP multicast group.
            # This  tells the network interface to deliver multicast
            # datagrams destined to this multicast group.
            for ip in ip_addresses:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock_list.append(s)
                mreq = struct.pack('4s4s', socket.inet_aton(mcast_group),
                               socket.inet_aton(ip))
                try:
                    s.setsockopt(socket.IPPROTO_IP,
                                socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError as e:
                    logger.exception(f'{ip}: {e}')
                    return

            # Use a RcvFromSocket socket for receiving.
            sock = RcvFromSocket(socket.AF_INET, socket.SOCK_DGRAM,
                                 bufsize=8192)
            sock.setblocking(False)

            # Allow other processes to bind to the same multicast group
            # and port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Bind to the multicast (group, port).
            # Binding to (INADDR_ANY, port) would also work, except
            # that in that case the socket would also receive the datagrams
            # destined to (any other address, mcast_port).
            sock.bind((mcast_group, mcast_port))

            while True:
                datagram, src_addr = await sock.recvfrom()
                self._handle_notify(datagram, src_addr[0])

        except OSError as e:
            logger.exception(f'{e}')
        finally:
            logger.debug('end of ssdp notify task')
            if sock is not None:
                sock.close()
            for s in sock_list:
                s.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
