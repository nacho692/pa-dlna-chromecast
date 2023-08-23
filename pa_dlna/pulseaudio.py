"""The pulseaudio interface."""

import asyncio
import logging

from .pulselib import (PulseLib, PA_SUBSCRIPTION_MASK_SINK_INPUT,
                       PulseMissingLibError, PulseStateError)
from .upnp.util import NL_INDENT, log_exception

logger = logging.getLogger('pulse')

# Classes.
class NullSink:
    """A connection between a sink_input and the null-sink of a Renderer.

    A NullSink is instantiated upon registering a Renderer instance.
    """

    def __init__(self, sink):
        self.sink = sink        # pulselib Sink instance
        self.sink_input = None  # pulselib SinkInput instance

class Pulse:
    """Pulse monitors pulseaudio sink-input events."""

    def __init__(self, av_control_point):
        self.av_control_point = av_control_point
        self.closing = False
        self.pulse_lib = None

    async def close(self):
        if not self.closing:
            self.closing = True
            logger.info('Close pulse')
            await self.av_control_point.close()

    async def register(self, renderer):
        """Load a null-sink module."""

        if self.pulse_lib is None:
            return

        root_device = renderer.root_device
        module_name = f'{root_device.modelName}-{root_device.udn}'
        _description = renderer.description.replace(' ', r'\ ')

        module_index = await self.pulse_lib.pa_context_load_module(
            'module-null-sink',
            f'sink_name="{module_name}" '
            f'sink_properties=device.description=' f'"{_description}"')

        # Return the NullSink instance.
        for sink in await self.pulse_lib.pa_context_get_sink_info_list():
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
                    await self.pulse_lib.pa_context_unload_module(module_index)
                    renderer.control_point.abort(
                        f'Two DLNA devices registered with the same name:'
                        f'{NL_INDENT}{module_name}')

                return NullSink(sink)

        await self.pulse_lib.pa_context_unload_module(module_index)
        logger.error(
            f'Failed loading {module_name} pulseaudio module')
        return None

    async def unregister(self, nullsink):
        if self.pulse_lib is None:
            return
        logger.info(f'Unload null-sink module {nullsink.sink.name}')
        await self.pulse_lib.pa_context_unload_module(
                                                nullsink.sink.owner_module)

    async def get_sink_input(self, renderer):
        assert renderer.nullsink is not None
        sink_inputs = (await
                       self.pulse_lib.pa_context_get_sink_input_info_list())
        for sink_input in sink_inputs:
            if sink_input.sink == renderer.nullsink.sink.index:
                return sink_input
        return None

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
        sink_inputs = (await
                       self.pulse_lib.pa_context_get_sink_input_info_list())
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
        """Dispatch the event to a renderer.

        event.type is either 'new', 'change' or 'remove'.
        A new event.index is generated by pulseaudio for each event of type
        'new' or 'change'. The index of a 'remove' event refers to the index
        of a previous 'new' or 'change' event.

        A 'new' event establishes an association between a sink-input and (to)
        a sink. A 'change' event signals a change in the metadata of the
        stream.

        IMPORTANT:
        'nullsink.sink' and 'nullsink.sink_input' are the renderer's instances
        built from one of the previous events. They are stale instances.
        'sink' and 'sink_input' returned by find_renderer() and
        get_sink_by_name() are the current instances as set by pulseaudio.
        """

        evt_type = event.type
        if evt_type == 'remove':
            renderer = self.find_previous_renderer(event)
            if renderer is not None:
                renderer.pulse_queue.put_nowait((evt_type, None, None))
            return

        renderer, sink_input = await self.find_renderer(event)
        if renderer is not None:
            # 'renderer.nullsink.sink' is the stale sink from the previous
            # event, we need to fetch the 'sink' correponding to this event.
            sink = await self.pulse_lib.pa_context_get_sink_info_by_name(
                                                renderer.nullsink.sink.name)
            if sink is not None:
                renderer.pulse_queue.put_nowait((evt_type, sink, sink_input))

        prev_renderer = self.find_previous_renderer(event)
        # The sink_input has been re-routed to another sink.
        if prev_renderer is not None and prev_renderer is not renderer:
            # Build our own 'exit' event (pulseaudio does not provide one)
            # for the sink that had been previously connected to this
            # sink_input.
            evt_type = 'exit'
            prev_renderer.pulse_queue.put_nowait((evt_type, None, None))

    @log_exception(logger)
    async def run(self):
        try:
            async with PulseLib('pa-dlna') as self.pulse_lib:
                logger.info('Connected to pulseaudio server')

                await self.pulse_lib.pa_context_subscribe(
                                    PA_SUBSCRIPTION_MASK_SINK_INPUT)
                iterator = self.pulse_lib.get_events()
                self.av_control_point.start_event.set()
                async for event in iterator:
                    await self.dispatch_event(event)

                # Wait until end of test.
                test_end = self.av_control_point.test_end
                if test_end is not None:
                    await test_end

        except (PulseMissingLibError, PulseStateError) as e:
            logger.error(f'{e!r}')
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            self.pulse_lib = None
            await self.close()
