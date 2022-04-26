"""The pulseaudio interface."""

import logging

logger = logging.getLogger('pulse')

class Pulseaudio:
    """Pulseaudio interface."""

    def __init__(self, aio_tasks):
        self.aio_tasks = aio_tasks
