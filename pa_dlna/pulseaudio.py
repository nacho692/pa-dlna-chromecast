"""The pulseaudio interface."""

import asyncio
import logging
from collections import namedtuple
from pulsectl_asyncio import PulseAsync
from pulsectl import PulseError, PulseEventMaskEnum, PulseEventTypeEnum

logger = logging.getLogger('pulse')

# A Playback instance is a connection of a sink-input to a sink.
Playback = namedtuple('Playback', ['sink_input', 'sink'])

# Class(es).
class PulseEvent:
    """A sink-input event."""

    def __init__(self, event_type, playback):
        self._playback = playback

        # self.event is either one of ('new', 'change', 'remove') from
        # pulsectl PulseEventTypeEnum or the 'switch' event that occurs when
        # the 'running' sink-input is switched to another sink.
        if event_type in PulseEventTypeEnum:
            self.event = event_type._value
        else:
            self.event = event_type
        self.index = playback.sink_input.index
        self.state = playback.sink.state._value

    def __str__(self):
        return (f"Sink-input {self.index} '{self.event}' event for"
                f" sink {self._playback.sink.name} state '{self.state}'")

class PulseAudio:
    """PulseAudio monitors pulseaudio events.

    A MediaRenderer instance registers with the PulseAudio instance to receive
    those events.
    """

    def __init__(self, av_control_point):
        self.av_control_point = av_control_point
        self.closed = False
        self.pulse_ctl = None
        self.playbacks = set()     # set of Playback instances
        self.renderers = {}        # {null-sink index: MediaRenderer}

    def close(self, exc=None):
        class SilenceAsyncio:
            def filter(self, record):
                return None

        if not self.closed:
            self.closed = True

            # Avoid error message upon KeyboardInterrupt:
            # "asyncio ERROR   Exception in callback Future.set_result(None)"
            # whose origin is in pulsectl_asyncio.
            logging.getLogger('asyncio').addFilter(SilenceAsyncio())

            errmsg = f'{exc!r}' if exc else None
            self.av_control_point.curtask.cancel(msg=errmsg)

            self.av_control_point.aio_tasks.cancel_all()
            logger.debug('PulseAudio is closed')

    async def register(self, renderer):
        """Register a MediaRenderer instance."""

        device = renderer.root_device
        if self.pulse_ctl is None:
            raise RuntimeError(f'Attempting to register'
                               f' "{device.friendlyName}" while'
                               f' not connected to pulseaudio')

        sinks = await self.pulse_ctl.sink_list()
        previous_idxs = (sink.index for sink in sinks)

        # Load a null-sink module.
        name = device.modelName
        description = device.friendlyName
        logger.debug(f'Load null-sink module, name="{name}"'
                     f' description="{description}"')

        name = name.replace(' ', r'\ ')
        description = description.replace(' ', r'\ ')
        module_index = await self.pulse_ctl.module_load('module-null-sink',
                                args=f'sink_name="{name}" '
                                     f'sink_properties=device.description='
                                     f'"{description}"')

        # Find the index of the null-sink.
        sinks = await self.pulse_ctl.sink_list()
        idxs = set(sink.index for sink in sinks)
        diff = idxs.difference(previous_idxs)
        if len(diff) != 1:
            raise RuntimeError(f'Got {len(diff)} more new sinks instead of 1,'
                               f' while loading a null-sink module')
        sink_index = diff.pop()
        renderer.sink_index = sink_index
        renderer.module_index = module_index
        self.renderers[sink_index] = renderer

    async def unregister(self, renderer):
        if self.pulse_ctl is None:
            return

        assert renderer.sink_index in self.renderers
        logger.debug(f'Unload null-sink module'
                     f' name="{renderer.root_device.modelName}"')
        await self.pulse_ctl.module_unload(renderer.module_index)
        del self.renderers[renderer.sink_index]

    async def dispatch(self, event_type, playback):
        """Dispatch an event to a registered MediaRenderer instance."""

        renderer = self.renderers.get(playback.sink.index)
        if renderer is not None:
            event = PulseEvent(event_type, playback)
            logger.info(f'{event}')
            await renderer.on_pulse_event(event)

    def remove_playback(self, index):
        for playback in list(self.playbacks):
            if playback.sink_input.index == index:
                self.playbacks.remove(playback)
                return playback
        return None

    async def handle_event(self, event):
        """Dispatch the event."""

        # A 'remove' event.
        if event.t == PulseEventTypeEnum.remove:
            previous = self.remove_playback(event.index)
            if previous is not None:
                await self.dispatch(event.t, previous)
            return

        # Find the sink_input that has triggered the event.
        # Note that by the time this code is running, pulseaudio may have done
        # other changes. In other words, there may be inconsistencies between
        # the event and the sink_input and sink lists.
        sink_inputs = await self.pulse_ctl.sink_input_list()
        for sink_input in sink_inputs:
            index = sink_input.index

            if index == event.index:
                # Ignore 'pulsesink probe' - seems to be used to query sink
                # formats (not for playback).
                if sink_input.name == 'pulsesink probe':
                    return

                # Find the corresponding sink and instantiate a Playback.
                sinks = await self.pulse_ctl.sink_list()
                for sink in sinks:
                    if sink.index == sink_input.sink:
                        previous = self.remove_playback(index)
                        playback = Playback(sink_input, sink)
                        self.playbacks.add(playback)

                        if previous is not None:
                            # The sink_input/sink connection has not changed.
                            if previous.sink_input.sink == sink_input.sink:
                                # We are only interested in changes to the
                                # sink state.
                                if previous.sink.state != sink.state:
                                    await self.dispatch(event.t, playback)

                            # The sink_input has been re-routed to another
                            # sink.
                            else:
                                await self.dispatch(event.t, playback)
                                # Build a new 'switch' event for the sink that
                                # had been previously connected to this
                                # sink_input.
                                await self.dispatch('switch', previous)
                        else:
                            await self.dispatch(event.t, playback)

    async def run(self):
        try:
            async with PulseAsync('pa-dlna') as self.pulse_ctl:
                self.av_control_point.event.set()
                try:
                    async for event in self.pulse_ctl.subscribe_events(
                                                PulseEventMaskEnum.sink_input):
                        await self.handle_event(event)
                finally:
                    # Unload the null-sink modules.
                    for rndr in self.renderers.values():
                        await self.pulse_ctl.module_unload(rndr.module_index)
                    self.pulse_ctl = None
        except asyncio.CancelledError:
            self.close()
            raise
        except KeyboardInterrupt as e:
            logger.debug('PulseAudio.run() got KeyboardInterrupt')
            self.close(exc=e)
        except PulseError as e:
            logger.error(f'{e!r}')
            self.close(exc=e)
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close(exc=e)
