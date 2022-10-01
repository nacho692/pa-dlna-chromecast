"""The pulseaudio interface."""

import asyncio
import logging
from pulsectl_asyncio import PulseAsync
from pulsectl import PulseEventMaskEnum, PulseEventTypeEnum

from .upnp import NL_INDENT

logger = logging.getLogger('pulse')

async def sink_unique_name(name_prefix, pulse_ctl):
    """Return a sink unique name.

    Unique sink names have the form 'name_prefix-suffix'.
    'suffix' is an integer, incremented by sink_unique_name() when
    'name_prefix' is already used by one of the listed sinks.
    No suffix is appended when none of the current listed sinks is using
    'name_prefix'.
    """

    names_suffixes = {}
    for sink in await pulse_ctl.sink_list():
        try:
            name, suffix = sink.name.rsplit('-', maxsplit=1)
            suffix = int(suffix)
        except AttributeError:
            continue
        except ValueError:
            name = sink.name
            suffix = 1
        if name in names_suffixes:
            if suffix > names_suffixes[name]:
                names_suffixes[name] = suffix
        else:
            names_suffixes[name] = suffix

    if name_prefix in names_suffixes:
        suffix = names_suffixes[name_prefix] + 1
        return f'{name_prefix}-{suffix}'
    else:
        return name_prefix

def log_pulse_event(event, renderer, sink=None, sink_input=None):
    if sink is None:
        sink = renderer.nullsink.sink
        sink_state = f'previous state: {sink.state._value}'
    else:
        prev_sink = renderer.nullsink.sink
        prev_state = prev_sink.state._value if prev_sink is not None else None
        new_state = sink.state._value
        sink_state = f'prev/new state: {prev_state}/{new_state}'

    if sink_input is None:
        sink_input = renderer.nullsink.sink_input

    logger.debug(f"'{event}' pulseaudio event [{renderer.name} "
                 f'sink: idx {sink_input.index}, {sink_state}]')

# Classes.
class NullSink:
    """A connection between a sink_input and the null-sink of a Renderer.

    A NullSink is instantiated upon registering a Renderer instance.
    """

    def __init__(self, sink, module_index):
        self.sink = sink                    # a pulse_ctl sink instance
        self.module_index = module_index    # index of the null-sink module
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

        device = renderer.root_device
        name = device.modelName.replace(' ', r'_')
        name = await sink_unique_name(name, self.pulse_ctl)

        description = device.friendlyName
        _description = description.replace(' ', r'\ ')

        module_index = await self.pulse_ctl.module_load('module-null-sink',
                                args=f'sink_name="{name}" '
                                     f'sink_properties=device.description='
                                     f'"{_description}"')

        # Find the index of the null-sink.
        for sink in await self.pulse_ctl.sink_list():
            if sink.name == name:
                logger.info(f"Load null-sink module '{name}',"
                            f" description='{description}'")
                return NullSink(sink, module_index)

        await self.pulse_ctl.module_unload(module_index)
        logger.error('Cannot find the index of the created null-sink')

    async def unregister(self, renderer):
        if self.pulse_ctl is None:
            return
        logger.info(f'Unload null-sink module'
                    f" '{renderer.nullsink.sink.name}'")
        await self.pulse_ctl.module_unload(renderer.nullsink.module_index)

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

    async def handle_event(self, event):
        """Dispatch the event."""

        evt = event.t._value
        if event.t == PulseEventTypeEnum.remove:
            renderer = self.find_previous_renderer(event)
            if renderer is not None:
                log_pulse_event(evt, renderer)
                await renderer.on_pulse_event(evt)
            return

        renderer, sink_input = await self.find_renderer(event)
        if renderer is not None:
            sink = await self.pulse_ctl.get_sink_by_name(
                                            renderer.nullsink.sink.name)
            log_pulse_event(evt, renderer, sink, sink_input)
            await renderer.on_pulse_event(evt, sink, sink_input)

        previous = self.find_previous_renderer(event)
        # The sink_input has been re-routed to another sink.
        if previous is not None and previous is not renderer:
            # Build our own 'exit' event (pulseaudio does not provide one)
            # for the sink that had been previously connected to this
            # sink_input.
            evt = 'exit'
            log_pulse_event(evt, previous)
            await previous.on_pulse_event(evt)

    async def run(self):
        try:
            async with PulseAsync('pa-dlna') as self.pulse_ctl:
                self.av_control_point.start_event.set()
                try:
                    async for event in self.pulse_ctl.subscribe_events(
                                                PulseEventMaskEnum.sink_input):
                        await self.handle_event(event)
                except Exception as e:
                    logger.exception(f'{e!r}')
                    await self.close()
                finally:
                    self.pulse_ctl = None
        except asyncio.CancelledError:
            await self.close()
        except Exception as e:
            if (hasattr(e, '__cause__') and
                    'pulse errno 6' in str(e.__cause__)):
                # 'Failed to connect to pulseaudio server' without backtrace.
                logger.error(f'{e!r}')
            else:
                logger.exception(f'{e!r}')
            await self.close()
