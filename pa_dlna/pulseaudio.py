"""The pulseaudio interface."""

import asyncio
import logging
import pulsectl
from pulsectl_asyncio import PulseAsync

from .upnp.util import NL_INDENT, log_exception

logger = logging.getLogger('pulse')

# Classes.
class NullSink:
    """A connection between a sink_input and the null-sink of a Renderer.

    A NullSink is instantiated upon registering a Renderer instance.
    """

    def __init__(self, sink):
        self.sink = sink                    # a pulse_ctl sink instance
        self.sink_input = None              # a pulse_ctl sink-input instance

class Pulse:
    """Pulse monitors pulseaudio sink-input events."""

    def __init__(self, av_control_point):
        self.av_control_point = av_control_point
        self.closing = False
        self.pulse_ctl = None

    async def close(self):
        if not self.closing:
            self.closing = True
            await self.av_control_point.close()
            logger.info('Close pulse')

    async def register(self, renderer):
        """Load a null-sink module."""

        if self.pulse_ctl is None:
            return

        root_device = renderer.root_device
        module_name = f'{root_device.modelName}-{root_device.udn}'
        _description = renderer.description.replace(' ', r'\ ')

        module_index = await self.pulse_ctl.module_load('module-null-sink',
                                args=f'sink_name="{module_name}" '
                                     f'sink_properties=device.description='
                                     f'"{_description}"')

        # Return the NullSink instance.
        for sink in await self.pulse_ctl.sink_list():
            if sink.owner_module == module_index:
                logger.info(f'Load null-sink module {sink.name}'
                        f"{NL_INDENT}description='{renderer.description}'")

                # The module name is registered by pulseaudio after being
                # modified in pa_namereg_register() by replacing invalid
                # characters with '_'. The invalid characters are defined in
                # is_valid_char(char c).See the pulseaudio code.
                if len(module_name) != len(sink.name):
                    # Pulseaudio has added a '.n' suffix because there exists
                    # another null-sink with the same name.
                    await self.pulse_ctl.module_unload(module_index)
                    renderer.control_point.abort(
                        f'Two DLNA devices registered with the same name:'
                        f'{NL_INDENT}{module_name}')

                return NullSink(sink)

        await self.pulse_ctl.module_unload(module_index)
        logger.error(
            f'Failed loading {module_name} pulseaudio module')
        return None

    async def unregister(self, nullsink):
        if self.pulse_ctl is None:
            return
        logger.info(f'Unload null-sink module {nullsink.sink.name}')
        await self.pulse_ctl.module_unload(nullsink.sink.owner_module)

    def find_previous_renderer(self, event):
        """Find the renderer that was last connected to this sink-input."""

        for renderer in self.av_control_point.renderers:
            if (renderer.nullsink is not None and
                    renderer.nullsink.sink_input is not None and
                    renderer.nullsink.sink_input.index == event.index):
                return renderer

    async def find_renderer(self, event):
        """Find the renderer now connected to this sink-input."""

        notfound = (None, None)

        # Find the sink_input that has triggered the event.
        # Note that by the time this code is running, pulseaudio may have done
        # other changes. In other words, there may be inconsistencies between
        # the event and the sink_input and sink lists.
        sink_inputs = await self.pulse_ctl.sink_input_list()
        for sink_input in sink_inputs:
            if sink_input.index == event.index:
                # Ignore 'pulsesink probe' - seems to be used to query sink
                # formats (not for playback).
                if sink_input.name == 'pulsesink probe':
                    return notfound

                # Find the corresponding sink when it is the null-sink of a
                # Renderer.
                for renderer in self.av_control_point.renderers:
                    if (renderer.nullsink is not None and
                            renderer.nullsink.sink.index == sink_input.sink):
                        return renderer, sink_input
                break
        return notfound

    async def dispatch_event(self, event):
        """Dispatch the event to a renderer."""

        evt = event.t._value
        if event.t == pulsectl.PulseEventTypeEnum.remove:
            renderer = self.find_previous_renderer(event)
            if renderer is not None:
                renderer.pulse_queue.put_nowait((evt, None, None))
            return

        renderer, sink_input = await self.find_renderer(event)
        if renderer is not None:
            sink = await self.pulse_ctl.get_sink_by_name(
                                            renderer.nullsink.sink.name)
            if sink is not None:
                renderer.pulse_queue.put_nowait((evt, sink, sink_input))

        prev_renderer = self.find_previous_renderer(event)
        # The sink_input has been re-routed to another sink.
        if prev_renderer is not None and prev_renderer is not renderer:
            # Build our own 'exit' event (pulseaudio does not provide one)
            # for the sink that had been previously connected to this
            # sink_input.
            evt = 'exit'
            prev_renderer.pulse_queue.put_nowait((evt, None, None))

    @log_exception(logger)
    async def run(self):
        pulse_connected = False
        first_attempt = True
        try:
            while True:
                try:
                    async with PulseAsync('pa-dlna') as self.pulse_ctl:
                        logger.info('Connected to pulseaudio server')
                        pulse_connected = True
                        self.av_control_point.start_event.set()

                        async for event in self.pulse_ctl.subscribe_events(
                                    pulsectl.PulseEventMaskEnum.sink_input):
                            await self.dispatch_event(event)

                        logger.warning('Unexpected exit from pulse event loop')
                        break

                except Exception as e:
                    # Failed to connect to pulseaudio server.
                    if (not pulse_connected and hasattr(e, '__cause__') and
                            'pulse errno 6' in str(e.__cause__)):
                        if first_attempt:
                            first_attempt = False
                            logger.info(
                                'Waiting to connect to pulseaudio server')
                        await asyncio.sleep(1)
                    else:
                        raise

        except pulsectl.PulseDisconnected as e:
            logger.error(f'Pulseaudio error: {e!r}')
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            self.pulse_ctl = None
            await self.close()
