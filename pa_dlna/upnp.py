"""A simple asyncio upnp library.

The library API is defined a follows:
  - Instantiate an Upnp object and create the Upnp.run() asyncio task.
  - Receive 'alive' notifications on the Upnp.queue asyncio queue,
    the notification holds a weak reference to an UpnpDevice instance.
    Weak references are used to avoid a race condition that occurs if the
    Upnp.queue were used to signal a 'byebye' notification.
  - One may register with Upnp.register_cb() a call back to be used by
    the Upnp instance when creating the weak reference.
"""

import asyncio
import logging
import weakref
import socket
import struct

logger = logging.getLogger('upnp')
mcast_group = '239.255.255.250'
mcast_port = 1900

class Upnp:

    def __init__(self, loop, ip_addresses, ttl):
        self.loop = loop
        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self.queue = asyncio.Queue()
        self.devices = {}
        self.callback = None

    def register_cb(self, callback):
        """Callback to be called when a device is about to be finalized."""
        self.callback = callback

    def new_device(self, udn):
        device = None # XXX instantiate device
        self.devices[udn] = device
        return weakref.ref(device, self.callback)

    async def msearch(self):
        try:
            await asyncio.sleep(1)
        except Exception as e:
            logger.exception(e)
        finally:
            logger.info('end of task msearch')

    async def listen_notifications(self):
        """Listen on SSDP notifications."""

        # See section 5.10.2 Receiving IP Multicast Datagrams
        # in "An Advanced 4.4BSD Interprocess Communication Tutorial".
        # See also https://tldp.org/HOWTO/Multicast-HOWTO-6.html.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)

            # Become a member of the IP multicast group.
            mreq = struct.pack('4sL', socket.inet_aton(mcast_group),
                               socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            # Allow other processes to bind to the same multicast group
            # and port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(('', mcast_port))

            try:
                datagram = await self.loop.sock_recv(sock, 8192)

                #XXX check that the sender is in ip_addresses
                await asyncio.sleep(100)
            except OSError as e:
                logger.exception(e)
        finally:
            sock.close()

    async def run(self):
        # Start the msearch task.
        msearch_t = asyncio.create_task(self.msearch(), name='msearch')

        try:
            await self.listen_notifications()
        except Exception as e:
            logger.exception(e)
        finally:
            logger.info('end of task upnp')
