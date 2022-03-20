"""A simple asyncio upnp library.
"""

import asyncio
import logging

logger = logging.getLogger('upnp')

class Upnp:

    def __init__(self, ip_addresses, ttl):
        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self.devices = {}
        self.queue = asyncio.Queue()

    async def run(self):
        try:
            await asyncio.sleep(10)
        finally:
            pass # cancel all tasks

        # XXX start one shot task: msearch_t
