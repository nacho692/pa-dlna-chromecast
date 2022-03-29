"""A simple asyncio upnp library.

The library API is defined a follows:

  - Instantiate an Upnp object and run the Upnp.run() coroutine.
  - Await on Upnp.get_notification() to get 'alive' or 'byebye' notifications
    with the corresponding UpnpDevice instance.
"""

import asyncio
import logging
import socket
import struct

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

    header_dict = http_header_as_dict(header)
    # Check the presence of some required keys.
    for key in ('NTS', 'USN'):
        if key not in header_dict:
            raise InvalidSsdpError(
                f'missing "{key}" field in SSDP notify:\n{header}')
    return header_dict

class AsyncioTasks:
    """Save references to tasks, to avoid tasks being garbage collected.

    See https://github.com/python/cpython/pull/29163 and the corresponding
    Python issues.
    """

    def __init__(self):
        self.tasks = set()              # references to tasks

    def create_task(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(lambda t: self.tasks.remove(t))
        return task

class RcvFromSocket(socket.socket):
    """Implementation of socket.recvfrom() as a coroutine.

    The buffer size is passed in the constructor as 'bufsize'.
    """

    def __init__(self, *args, **kwargs):
        if 'bufsize' in kwargs:
            self.bufsize = kwargs['bufsize']
            del kwargs['bufsize']
        else:
            self.bufsize = 8192
        super(RcvFromSocket, self).__init__(*args, **kwargs)
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.loop.add_reader(self.fileno(), self.reader)

    def reader(self):
        data, src_addr = super(RcvFromSocket, self).recvfrom(self.bufsize)
        self.queue.put_nowait((data, src_addr))

    async def recvfrom(self):
        # The size of the buffer is set in the constructor.
        return await self.queue.get()

    def close(self):
        self.loop.remove_reader(self.fileno())
        super(RcvFromSocket, self).close()

class UpnpDevice:
    """An upnp device."""

    def __init__(self, http_header, upnp):
        self.header = http_header
        self.upnp = upnp
        self.closed = False

    def close(self):
        self.closed = True

    async def run(self):
        try:
            # XXX do all the main work
            self.upnp.put_notification('alive', self)
        finally:
            logger.debug(f'end of the UpnpDevice task {self}')

    def __str__(self):
        """Return a short representation of udn."""
        return shorten(self.header['USN'].split('::')[0])

class Upnp:
    """Manage the notify and msearch tasks."""

    def __init__(self, ip_addresses, ttl):
        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self._queue = asyncio.Queue()
        self._devices = {}              # {udn: (UpnpDevice, its task)}
        self.aio_tasks = AsyncioTasks()

    async def get_notification(self):
        """Return the tuple ('alive' or 'byebye', UpnpDevice instance)."""
        return await self._queue.get()

    def put_notification(self, kind, upnpdevice):
        self._queue.put_nowait((kind, upnpdevice))
        state = 'created' if kind == 'alive' else 'deleted'
        logger.info(f'UpnpDevice {upnpdevice} has been {state}')

    def handle_notify(self, datagram, ip_source):
        # Ignore non SSDP notify datagrams.
        try:
           header = datagram.decode().splitlines()
        except UnicodeError:
            return
        if not header or header[0].strip() != 'NOTIFY * HTTP/1.1':
            if header:
                logger.debug(f"SSDP notify: ignoring '{header[0].strip()}'"
                             f' from {ip_source}')
            return

        # Parse the HTTP header as a dict.
        try:
            header = parse_notify_header(header)
        except InvalidSsdpError as e:
            logger.warning(f'SSDP notify: {e}')
            return

        nts = header['NTS']
        udn = header['USN'].split('::')[0]

        if nts == 'ssdp:alive':
            if udn not in self._devices:
                logger.info(f'SSDP notify: new UpnpDevice {shorten(udn)}')

                # Instantiate the UpnpDevice and starts its task.
                device = UpnpDevice(header, self)
                self.aio_tasks.create_task(device.run(), name=str(device))
                self._devices[udn] = device
            else:
                logger.debug(f'SSDP notify: ignoring duplicate {shorten(udn)}')

        elif nts == 'ssdp:byebye':
            device = self._devices.get(udn, None)
            if device is not None:
                device.close()
                self.put_notification('byebye', device)
                # XXX del self._devices[udn]
                # a device is never deleted, it is closed until new notification

        elif nts == 'ssdp:update':
            logger.warning(f'SSDP notify: ignoring not supported'
                           f' {nts} notification')

        else:
            logger.warning(f'SSDP notify: unknown NTS field "{nts}"'
                           f' in SSDP notify')

    async def ssdp_msearch(self):
        # See https://tldp.org/HOWTO/Multicast-HOWTO-6.html.
        try:
            await asyncio.sleep(1)
        except Exception as e:
            logger.exception(e)
        finally:
            logger.debug('end of msearch task')

    async def ssdp_notify(self, ip_addresses):
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
                    logger.exception(f'SSDP notify: {ip}: {e}')
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
                self.handle_notify(datagram, src_addr[0])

        except OSError as e:
            logger.exception(f'SSDP notify: {e}')
        finally:
            logger.debug('end of notify task')
            if sock is not None:
                sock.close()
            for s in sock_list:
                s.close()

    async def run(self):
        # Start the msearch task.
        msearch_t = self.aio_tasks.create_task(self.ssdp_msearch(),
                                               name='msearch')
        # Start the notify task.
        notify_t = self.aio_tasks.create_task(
            self.ssdp_notify(self.ip_addresses), name='notify')

        try:
            await asyncio.wait((msearch_t, notify_t),
                                        return_when=asyncio.ALL_COMPLETED)
        finally:
            logger.debug('end of upnp task')
