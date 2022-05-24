"""An Upnp control point that forwards Pulseaudio streams to DLNA devices."""

import asyncio
import logging
import re
from collections import namedtuple
from pulsectl_asyncio import PulseAsync
from pulsectl import (PulseError, PulseEventMaskEnum, PulseEventTypeEnum,
                      PulseStateEnum)

from . import (main_function, UPnPApplication)
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

# Set 'test_pulseaudio' to True for testing when no DLNA device is available.
test_pulseaudio = 0

MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

# A Playback instance is a connection of a sink-input to a sink.
Playback = namedtuple('Playback', ['sink_input', 'sink'])

# Utilities.
def log_event(event_type, playback):
    i = playback.sink_input
    s = playback.sink
    logger.info(f"Sink-input {i.index} '{event_type._value}' event for sink"
                f" {s.name}({s.index}) state '{s.state._value}'")

# Class(es).
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
        self.renderers = {}        # {null-sink module index: MediaRenderer}

    def close(self, exc=None):
        if not self.closed:
            self.closed = True

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
        index = await self.pulse_ctl.module_load('module-null-sink',
                                args=f'sink_name="{name}" '
                                     f'sink_properties=device.description='
                                     f'"{description}"')
        self.renderers[index] = renderer

        sinks = await self.pulse_ctl.sink_list()
        idxs = set(sink.index for sink in sinks)
        diff = idxs.difference(previous_idxs)
        if len(diff) != 1:
            raise RuntimeError(f'Got {len(diff)} more sink(s) instead of 1,'
                               f' while loading a null-sink module')
        return diff.pop()

    async def unregister(self, renderer):
        if self.pulse_ctl is not None:
            for index, rnd in list(self.renderers.items()):
                if rnd is renderer:
                    logger.debug(f'Unload null-sink module'
                                 f' name="{rnd.modelName}"')
                    await self.pulse_ctl.module_unload(index)
                    del self.renderers[index]

    async def dispatch(self, event_type, playback):
        """Dispatch an event to a registered MediaRenderer instance."""

        if playback is None:
            return

        for renderer in self.renderers.values():
            if renderer.sink_index == playback.sink.index:
                log_event(event_type, playback)
                await renderer.on_pulse_event(event_type, playback)
                break

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
                                # We are interested in changes to the state
                                # and media.name.
                                if (previous.sink_input.proplist['media.name']
                                        != sink_input.proplist['media.name'] or
                                        previous.sink.state != sink.state):
                                    await self.dispatch(event.t, playback)

                            # The sink_input has been re-routed to another
                            # sink.
                            else:
                                await self.dispatch(event.t, playback)
                                # Build a new 'remove' event for the sink that
                                # had been previously connected to this
                                # sink_input.
                                await self.dispatch(PulseEventTypeEnum.remove,
                                                    previous)
                        else:
                            await self.dispatch(event.t, playback)

    async def run(self):
        try:
            async with PulseAsync('pa-dlna') as self.pulse_ctl:
                try:
                    if test_pulseaudio:
                        await TestMediaRenderer(self.av_control_point).run()

                    async for event in self.pulse_ctl.subscribe_events(
                                                PulseEventMaskEnum.sink_input):
                        await self.handle_event(event)
                finally:
                    # Unload the null-sink modules.
                    for idx in self.renderers:
                        await self.pulse_ctl.module_unload(idx)
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

class MediaRenderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP) specifications:
      AVTransport:3 Service
      RenderingControl:3 Service
      ConnectionManager:3 Service
    """

    def __init__(self, root_device, av_control_point):
        self.root_device = root_device
        self.av_control_point = av_control_point
        self.sink_index = None          # null-sink index

    def close(self):
        self.root_device.close()

    async def on_pulse_event(self, event_type, playback):
        """Handle a Pulseaudio event."""

        assert self.sink_index == playback.sink.index

    async def soap_action(self, serviceId, action, args):
        """Send a SOAP action.

        Return the dict {argumentName: out arg value} if successfull,
        otherwise an instance of the upnp.xml.SoapFault namedtuple defined by
        field names in ('errorCode', 'errorDescription').
        """

        try:
            service = self.root_device.serviceList[serviceId]
            return await service.soap_action(action, args)
        except UPnPSoapFaultError as e:
            return e.args[0]
        except UPnPClosedDeviceError:
            logger.error(f'soap_action(): root device {self.root_device} is'
                         f' closed')
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close()

    async def run(self):
        """Set up the MediaRenderer."""

        resp = await self.soap_action(CONNECTIONMANAGER, 'GetProtocolInfo',
                                      {})
        pulse = self.av_control_point.pulse
        self.sink_index = await pulse.register(self)

class TestMediaRenderer(MediaRenderer):
    """MediaRenderer to be used for testing when no DLNA device available."""

    class RootDevice:
        modelName = 'R-N402D'
        friendlyName = 'Yamaha RN402D'

    def __init__(self, av_control_point):
        super().__init__(self.RootDevice(), av_control_point)

    def close(self):
        pass

    async def on_pulse_event(self, event_type, playback):
        pass

    async def run(self):
        pulse = self.av_control_point.pulse
        self.sink_index = await pulse.register(self)

class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage Pulseaudio and the DLNA MediaRenderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False
        self.devices = {}               # dict {UPnPDevice: MediaRenderer}
        self.curtask = None             # task running run_control_point()
        self.pulse = None               # PulseAudio instance
        self.aio_tasks = AsyncioTasks()

    def close(self):
        if not self.closed:
            self.closed = True
            for renderer in self.devices.values():
                renderer.close()
            self.devices = {}
            self.aio_tasks.cancel_all()

    def remove_device(self, device):
        if device in self.devices:
            del self.devices[device]

    async def run_control_point(self):
        try:
            self.curtask = asyncio.current_task()

            # Run the UPnP control point.
            async with UPnPControlPoint(self.ip_list,
                                        self.ttl) as control_point:
                # Create the Pulseaudio task.
                self.pulse = PulseAudio(self)
                self.aio_tasks.create_task(self.pulse.run(), name='pulseaudio')

                # Handle UPnP notifications.
                while True:
                    notif, root_device = await control_point.get_notification()
                    logger.info(f'Got notification'
                                f' {(notif, root_device)}')

                    # Ignore non MediaRenderer devices.
                    if re.match(rf'{MEDIARENDERER}(1|2)',
                                root_device.deviceType) is None:
                        continue

                    if notif == 'alive':
                        if root_device not in self.devices:
                            renderer = MediaRenderer(root_device, self)
                            await renderer.run()
                            self.devices[root_device] = renderer
                    else:
                        self.remove_device(root_device)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'Got exception {e!r}')
        finally:
            self.close()

    def __str__(self):
        return 'pa-dlna'

# The main function.
if __name__ == '__main__':
    main_function(AVControlPoint, __doc__, logger)
