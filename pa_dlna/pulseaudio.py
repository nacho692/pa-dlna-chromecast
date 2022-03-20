"""The interface to pulseaudio.
"""

import asyncio
import logging

logger = logging.getLogger('pulse')

class Pulseaudio:

    def __init__(self, queue):
        self.upnp_queue = queue

    async def run(self):
        try:
            await asyncio.sleep(100)
        finally:
            pass # cancel all tasks
