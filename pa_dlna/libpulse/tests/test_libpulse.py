"""libPulse test cases."""

import re
import asyncio
import logging
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from ...upnp.tests import load_ordered_tests

import pa_dlna.libpulse.libpulse as libpulse_module
from ..libpulse import *
from ...tests import requires_resources
from ...upnp.tests import search_in_logs

SINK_NAME= 'foo'
MODULE_ARG = (f'sink_name="{SINK_NAME}" sink_properties=device.description='
              f'"{SINK_NAME}\ description"')

async def get_event(facility, type, lib_pulse, ready):
    try:
        await lib_pulse.pa_context_subscribe(PA_SUBSCRIPTION_MASK_ALL)
        iterator = lib_pulse.get_events()
        ready.set_result(True)

        index = None
        async for event in iterator:
            if event.facility == facility and event.type == type:
                iterator.close()
                index = event.index
        return index
    except asyncio.CancelledError:
        print('get_event(): CancelledError')
    except LibPulseError as e:
        return e

class LoadModule:
    def __init__(self, lib_pulse, name, argument):
        self.lib_pulse = lib_pulse
        self.name = name
        self.argument = argument
        self.module_index = PA_INVALID_INDEX

    async def __aenter__(self):
        self.module_index = await self.lib_pulse.pa_context_load_module(
                                                self.name, self.argument)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self.module_index != PA_INVALID_INDEX:
            await self.lib_pulse.pa_context_unload_module(self.module_index)

@requires_resources('libpulse')
class LibPulseTestCase(IsolatedAsyncioTestCase):
    async def test_log_server_info(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            async with LibPulse('libpulse-test') as lib_pulse:
                await lib_pulse.log_server_info()

        self.assertTrue(search_in_logs(m_logs.output, 'libpuls',
                    re.compile(fr'Server: [Pp]ulse[Aa]udio.* \d+\.\d')))

    async def test_load_module(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            async with LibPulse('libpulse-test') as lib_pulse:
                async with LoadModule(lib_pulse, 'module-null-sink',
                                      MODULE_ARG) as loaded_module:
                    pass

        self.assertTrue(search_in_logs(m_logs.output, 'libpuls',
                    re.compile(f"Load 'module-null-sink'.*{SINK_NAME}.*"
                               f'description')))
        self.assertTrue(search_in_logs(m_logs.output, 'libpuls',
                    re.compile(f'Unload module at index'
                               f' {loaded_module.module_index}')))

    async def test_list_sinks(self):
        async with LibPulse('libpulse-test') as lib_pulse:
            async with LoadModule(lib_pulse, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                for sink in \
                        await lib_pulse.pa_context_get_sink_info_list():
                    if sink.name == SINK_NAME:
                        break
                else:
                    self.fail(f"'{SINK_NAME}' is not listed in the sink"
                              f' list')

    async def test_sink_by_name(self):
        async with LibPulse('libpulse-test') as lib_pulse:
            async with LoadModule(lib_pulse, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                sink = (await
                    lib_pulse.pa_context_get_sink_info_by_name(SINK_NAME))
                self.assertEqual(sink.name, SINK_NAME)

    async def test_sink_proplist(self):
        async with LibPulse('libpulse-test') as lib_pulse:
            async with LoadModule(lib_pulse, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                sink = (await
                    lib_pulse.pa_context_get_sink_info_by_name(SINK_NAME))
                self.assertTrue(re.match(fr'{SINK_NAME}\\? description',
                                sink.proplist['device.description']))

    async def test_events(self):
        async with LibPulse('libpulse-test') as lib_pulse:
            ready = lib_pulse.loop.create_future()
            evt_task = asyncio.create_task(get_event('module', 'new',
                                                     lib_pulse, ready))
            await ready

            async with LoadModule(lib_pulse, 'module-null-sink',
                                  MODULE_ARG) as loaded_module:
                await asyncio.wait_for(evt_task, 1)
                self.assertEqual(evt_task.result(),
                                 loaded_module.module_index)

    async def test_abort_iterator(self):
        async def main():
            try:
                async with LibPulse('libpulse-test') as lib_pulse:
                    # Run the asynchronous iterator loop until it is aborted
                    # or cancelled.
                    ready = lib_pulse.loop.create_future()
                    evt_task = asyncio.create_task(get_event('invalid', 'new',
                                                            lib_pulse, ready))
                    await ready
                    # Raise an exception to force the closing of the LibPulse
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
        libpulse_module.build_libpulse_prototypes()
        with mock.patch.object(libpulse_module,
                               'pa_context_connect') as connect,\
                self.assertRaises(PulseStateError):
            connect.side_effect = PulseStateError()
            async with LibPulse('libpulse-test') as lib_pulse:
                pass

    async def test_cancel_ctx_mgr(self):
        libpulse_module.build_libpulse_prototypes()
        with mock.patch.object(libpulse_module,
                               'pa_context_connect') as connect,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            connect.side_effect = asyncio.CancelledError()
            try:
                async with LibPulse('libpulse-test') as lib_pulse:
                    pass
            except PulseStateError as e:
                self.assertEqual(e.args[0], ('PA_CONTEXT_UNCONNECTED', 'PA_OK'))
            else:
                self.fail('PulseStateError has not been raised')

    async def test_cancel_main(self):
        async def main(main_ready):
            try:
                async with LibPulse('libpulse-test') as lib_pulse:
                    main_ready.set_result(True)
                    ready = lib_pulse.loop.create_future()
                    try:
                        await ready
                    except asyncio.CancelledError:
                        lib_pulse.state = error_state
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
            async with LibPulse('libpulse-test') as lib_pulse:
                LibPulse.ASYNCIO_LOOPS = dict()
                async with LoadModule(lib_pulse, 'module-null-sink',
                                      MODULE_ARG):
                    pass

    async def test_fail_connect(self):
        # This test assumes that the libpulse library calls
        # _context_state_callback() at least twice when connecting to the
        # library.
        libpulse_module.build_libpulse_prototypes()
        with mock.patch.object(libpulse_module,
                               'pa_context_get_state') as connect,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            connect.side_effect = [
                PA_CONTEXT_READY,   # connected
                PA_CONTEXT_READY,   # ignored state
                PA_CONTEXT_READY,   # idem
                PA_CONTEXT_FAILED,  # connection failure
            ]
            async with LibPulse('libpulse-test') as lib_pulse:
                wait_forever = lib_pulse.loop.create_future()
                try:
                    await wait_forever
                except asyncio.CancelledError:
                    # Ensure that lib_pulse._close() does call
                    # pa_context_disconnect().
                    lib_pulse.state = ('PA_CONTEXT_READY', 'PA_OK')
                else:
                    self.fail('wait_forever has not been cancelled as expected')
            self.assertTrue(search_in_logs(m_logs.output, 'libpuls',
                    re.compile('LibPulse instance .* aborted:.*PA_CONTEXT_FAILED')))

    async def test_missing_lib(self):
        # Force the reloading of the library.
        libpulse_module.build_libpulse_prototypes()
        if hasattr(libpulse_module, 'pa_context_new'):
            del libpulse_module.pa_context_new

        with mock.patch.object(libpulse_module,
                               'find_library') as find_library,\
                self.assertRaises(PulseMissingLibError):
            find_library.return_value = None
            async with LibPulse('libpulse-test') as lib_pulse:
                pass
