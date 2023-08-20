"""Pulselib test cases."""

import re
import asyncio
import logging
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from ...upnp.tests import load_ordered_tests

import pa_dlna.pulselib.pulselib as pulselib_module
from ..pulselib import *
from ...tests import requires_resources
from ...upnp.tests import search_in_logs

SINK_NAME= 'foo'
MODULE_ARG = (f'sink_name="{SINK_NAME}" sink_properties=device.description='
              f'"{SINK_NAME}\ description"')

async def get_event(facility, type, pulse_lib, ready):
    try:
        await pulse_lib.pa_context_subscribe(PA_SUBSCRIPTION_MASK_ALL)
        iterator = pulse_lib.get_events()
        ready.set_result(True)

        index = None
        async for event in iterator:
            if event.facility == facility and event.type == type:
                iterator.close()
                index = event.index
        return index
    except asyncio.CancelledError:
        print('get_event(): CancelledError')
    except PulseLibError as e:
        return e

class LoadModule:
    def __init__(self, pulse_lib, name, argument):
        self.pulse_lib = pulse_lib
        self.name = name
        self.argument = argument
        self.module_index = PA_INVALID_INDEX

    async def __aenter__(self):
        self.module_index = await self.pulse_lib.pa_context_load_module(
                                                self.name, self.argument)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self.module_index != PA_INVALID_INDEX:
            await self.pulse_lib.pa_context_unload_module(self.module_index)

@requires_resources('pulseaudio')
class PulseLibTestCase(IsolatedAsyncioTestCase):
    async def test_load_module(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            async with PulseLib('pulselib-test') as pulse_lib:
                async with LoadModule(pulse_lib, 'module-null-sink',
                                      MODULE_ARG) as loaded_module:
                    pass

        self.assertTrue(search_in_logs(m_logs.output, 'pulslib',
                    re.compile(f"Load 'module-null-sink'.*{SINK_NAME}.*"
                               f'description')))
        self.assertTrue(search_in_logs(m_logs.output, 'pulslib',
                    re.compile(f'Unload module at index'
                               f' {loaded_module.module_index}')))

    async def test_list_sinks(self):
        async with PulseLib('pulselib-test') as pulse_lib:
            async with LoadModule(pulse_lib, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                for sink in \
                        await pulse_lib.pa_context_get_sink_info_list():
                    if sink.name == SINK_NAME:
                        break
                else:
                    self.fail(f"'{SINK_NAME}' is not listed in the sink"
                              f' list')

    async def test_sink_by_name(self):
        async with PulseLib('pulselib-test') as pulse_lib:
            async with LoadModule(pulse_lib, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                sink = (await
                    pulse_lib.pa_context_get_sink_info_by_name(SINK_NAME))
                self.assertEqual(sink.name, SINK_NAME)

    async def test_sink_proplist(self):
        async with PulseLib('pulselib-test') as pulse_lib:
            async with LoadModule(pulse_lib, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                sink = (await
                    pulse_lib.pa_context_get_sink_info_by_name(SINK_NAME))
                self.assertEqual(sink.proplist['device.description'],
                                 f'{SINK_NAME} description')

    async def test_events(self):
        async with PulseLib('pulselib-test') as pulse_lib:
            ready = pulse_lib.loop.create_future()
            evt_task = asyncio.create_task(get_event('module', 'new',
                                                     pulse_lib, ready))
            await ready

            async with LoadModule(pulse_lib, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                await asyncio.wait_for(evt_task, 1)
                self.assertEqual(evt_task.result(),
                                 loaded_module.module_index)

    async def test_abort_iterator(self):
        async def main():
            try:
                async with PulseLib('pulselib-test') as pulse_lib:
                    # Run the asynchronous iterator loop until it is aborted
                    # or cancelled.
                    ready = pulse_lib.loop.create_future()
                    evt_task = asyncio.create_task(get_event('invalid', 'new',
                                                            pulse_lib, ready))
                    await ready
                    # Raise an exception to force the closing of the PulseLib
                    # instance and the iterator abort.
                    1/0
            except Exception as e:
                pass

            await evt_task
            return evt_task.result()

        main_task = asyncio.create_task(main())
        await main_task
        self.assertTrue(isinstance(main_task.result(),
                                   PulseClosedIteratorError))

    async def test_excep_ctx_mgr(self):
        with mock.patch.object(pulselib_module,
                               'pa_context_connect') as connect,\
                self.assertRaises(PulseStateError):
            connect.side_effect = PulseStateError()
            async with PulseLib('pulselib-test') as pulse_lib:
                pass

    async def test_cancel_ctx_mgr(self):
        with mock.patch.object(pulselib_module,
                               'pa_context_connect') as connect,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            connect.side_effect = asyncio.CancelledError()
            try:
                async with PulseLib('pulselib-test') as pulse_lib:
                    pass
            except PulseStateError as e:
                self.assertEqual(e.args[0], ('PA_CONTEXT_UNCONNECTED', 'PA_OK'))
            else:
                self.fail('PulseStateError has not been raised')

    async def test_cancel_main(self):
        async def main(main_ready):
            try:
                async with PulseLib('pulselib-test') as pulse_lib:
                    main_ready.set_result(True)
                    ready = pulse_lib.loop.create_future()
                    try:
                        await ready
                    except asyncio.CancelledError:
                        pulse_lib.state = error_state
                        raise
            except PulseStateError as e:
                return e
            except Exception:
                return None

        error_state = ('PA_CONTEXT_FAILED', 'PA_ERR_KILLED')
        loop = asyncio.get_running_loop()
        main_ready = loop.create_future()
        main_task = asyncio.create_task(main(main_ready))
        await main_ready
        main_task.cancel()
        await main_task
        result = main_task.result()
        self.assertTrue(isinstance(result, PulseStateError))
        self.assertEqual(result.args[0], error_state)

    async def test_fail_instance(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs,\
                self.assertRaises(PulseClosedError):
            async with PulseLib('pulselib-test') as pulse_lib:
                PulseLib.ASYNCIO_LOOPS = dict()
                async with LoadModule(pulse_lib, 'module-null-sink',
                                      MODULE_ARG):
                    pass

    async def test_fail_connect(self):
        # This test assumes that the pulselib library calls
        # _context_state_callback() at least twice when connecting to the
        # library.
        with mock.patch.object(pulselib_module,
                               'pa_context_get_state') as connect,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            connect.side_effect = [PA_CONTEXT_READY, PA_CONTEXT_FAILED]
            async with PulseLib('pulselib-test') as pulse_lib:
                wait_forever = pulse_lib.loop.create_future()
                try:
                    await wait_forever
                except asyncio.CancelledError:
                    # Ensure that pulse_lib._close() does call
                    # pa_context_disconnect().
                    pulse_lib.state = ('PA_CONTEXT_READY', 'PA_OK')
                else:
                    self.fail('wait_forever has not been cancelled as expected')
            self.assertTrue(search_in_logs(m_logs.output, 'pulslib',
                    re.compile('PulseLib instance .* aborted:.*PA_CONTEXT_FAILED')))

    async def test_missing_lib(self):
        # Force the reloading of the library.
        if hasattr(pulselib_module, 'pa_context_new'):
            del pulselib_module.pa_context_new

        with mock.patch.object(pulselib_module,
                               'find_library') as find_library,\
                self.assertRaises(PulseMissingLibError):
            find_library.return_value = None
            async with PulseLib('pulselib-test') as pulse_lib:
                pass
