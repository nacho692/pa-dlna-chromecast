"""The interface to pulseaudio.
"""

import asyncio
import logging

logger = logging.getLogger('pulse')

class Pulseaudio:

    def __init__(self, upnp):
        self.upnp = upnp

    async def run(self):
        try:
            await asyncio.sleep(100)
        finally:
            pass # cancel all tasks
