"""Testing the pulselib package."""

import asyncio
import logging

from pa_dlna.pulselib import *

logging.basicConfig(level=logging.DEBUG,
                    format='%(name)-7s %(levelname)-7s %(message)s')
logger = logging.getLogger('pulstst')

async def main():
    try:
        async with PulseLib('pa-dlna') as pulse_lib:
            logger.debug(f'main: connected')

            try:
                # Load/unload module.
                module_index = PA_INVALID_INDEX
                module_index = await pulse_lib.pa_context_load_module(
                    'module-null-sink',
                    f'sink_name="foo" sink_properties=device.description='
                    f'"foo\ description"')

                # List the sinks and sink inputs.
                for sink in await pulse_lib.pa_context_get_sink_info_list():
                    logger.debug(f'Sink: {sink.__dict__}')
                sink_input_list = pulse_lib.pa_context_get_sink_input_info_list
                for sink_input in await sink_input_list():
                    logger.debug(f'Sink input: {sink_input.__dict__}')

                # Get sink by name.
                sink = await pulse_lib.pa_context_get_sink_info_by_name('foo')
                logger.debug(f'Sink by name: {sink.__dict__}')

                # Events
                await pulse_lib.pa_context_subscribe(PA_SUBSCRIPTION_MASK_ALL)
                iterator = pulse_lib.get_events()
                async for event in iterator:
                    logger.debug(f'{event.facility}({event.index}):'
                                 f' {event.type}')

            finally:
                if module_index != PA_INVALID_INDEX:
                    await pulse_lib.pa_context_unload_module(module_index)

    except PulseLibError as e:
        logger.error(f'{e!r}')
    logger.info('FIN')

if __name__ == '__main__':
    asyncio.run(main())
