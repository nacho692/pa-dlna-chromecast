"""A simple asyncio upnp library.

The library API is defined a follows:
  - The client receives 'alive' notifications on the Upnp.queue asyncio queue,
    the notification holds a weak reference to an UpnpDevice instance. Weak
    references are used to avoid a race condition that occurs if the
    Upnp.queue were used to signal a 'byebye' notification.
  - The client may register with Upnp.register_cb() a call back to be used by
     the Upnp instance when creating the weak reference.
"""

import asyncio
import logging
import weakref

logger = logging.getLogger('upnp')

class Upnp:

    def __init__(self, ip_addresses, ttl):
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

    async def run(self):
        try:
            await asyncio.sleep(10)
        finally:
            pass # cancel all tasks

        # XXX start one shot task: msearch_t
