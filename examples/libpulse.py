"""Testing the libpulse package."""

import asyncio
import logging

from pa_dlna.libpulse import *

logging.basicConfig(level=logging.DEBUG,
                    format='%(name)-7s %(levelname)-7s %(message)s')
logger = logging.getLogger('pulstst')

async def log_events(lib_pulse, ready):
    try:
        await lib_pulse.pa_context_subscribe(PA_SUBSCRIPTION_MASK_ALL)
        iterator = lib_pulse.get_events()
        ready.set_result(True)

        async for event in iterator:
            logger.debug(f'{event.facility}({event.index}): {event.type}')
            if 0:
                # The iterator may be closed from the loop.
                iterator.close()
    except LibPulseError as e:
        logger.error(f'log_events(): {e!r}')

async def main():
    try:
        async with LibPulse('pa-dlna') as lib_pulse:
            logger.debug(f'main: connected')

            try:
                # Events
                ready = lib_pulse.loop.create_future()
                evt_task = asyncio.create_task(log_events(lib_pulse, ready))
                await ready

                # Load a module.
                module_index = PA_INVALID_INDEX
                module_index = await lib_pulse.pa_context_load_module(
                    'module-null-sink',
                    f'sink_name="foo" sink_properties=device.description='
                    f'"foo\ description"')

                # List the sinks and sink inputs.
                for sink in await lib_pulse.pa_context_get_sink_info_list():
                    logger.debug(f'Sink: {sink.__dict__}')
                sink_input_list = lib_pulse.pa_context_get_sink_input_info_list
                for sink_input in await sink_input_list():
                    logger.debug(f'Sink input: {sink_input.__dict__}')

                # Get sink by name.
                sink = await lib_pulse.pa_context_get_sink_info_by_name('foo')
                logger.debug(f'Sink by name: {sink.__dict__}')
                description = sink.proplist['device.description']
                logger.debug(f'Sink proplist.device.description:'
                             f" '{description}'")

                if 0:
                    await evt_task
                elif 1:
                    # The iterator is aborted upon closing the LibPulse
                    # instance.
                    time = 1
                    logger.info(f'main(): waiting {time} second(s)')
                    await asyncio.sleep(time)

            finally:
                if module_index != PA_INVALID_INDEX:
                    await lib_pulse.pa_context_unload_module(module_index)

    except LibPulseError as e:
        logger.error(f'main(): {e!r}')

if __name__ == '__main__':
    asyncio.run(main())
    logger.info('FIN')
