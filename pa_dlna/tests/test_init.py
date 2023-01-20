"""Command line test cases."""

import io
import struct
import logging
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, BaseTestCase
from ..init import parse_args, padlna_main, UPnPApplication
from ..encoders import Encoder
from ..config import user_config_pathname

@requires_resources('os.devnull')
class Init(BaseTestCase):
    def test_python_version(self):
        import sys
        import importlib
        import pa_dlna
        from .. import MIN_PYTHON_VERSION

        version = (MIN_PYTHON_VERSION[0], MIN_PYTHON_VERSION[1] - 1)
        try:
            with mock.patch.object(sys, 'version_info', version),\
                 redirect_stderr(io.StringIO()) as output,\
                 self.assertRaises(SystemExit) as cm:

                pa_dlna = importlib.reload(pa_dlna)

            self.assertEqual(cm.exception.args[0], 1)
            self.assertRegex(output.getvalue(),
                             f'^error.*{MIN_PYTHON_VERSION}')
        finally:
            pa_dlna = importlib.reload(pa_dlna)

@requires_resources('os.devnull')
class Argv(BaseTestCase):
    """Command line tests."""

    def test_no_args(self):
        options, _ = parse_args(self.__doc__, argv=[])
        self.assertEqual(options, {'dump_default': False,
                                   'dump_internal': False,
                                   'log_aio': False,
                                   'logfile': None,
                                   'loglevel': 'info',
                                   'msearch_interval': 60,
                                   'nics': [],
                                   'nolog_upnp': False,
                                   'port': 8080,
                                   'test_devices': [],
                                   'ttl': b'\x02'})

    def test_ttl(self):
        options, _ = parse_args(self.__doc__, argv=['--ttl', '255'])
        self.assertEqual(options['ttl'], b'\xff')

    def test_invalid_ttl(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--ttl', '256'])
        self.assertEqual(cm.exception.args[0], 2)
        self.assertTrue(isinstance(cm.exception.__context__, struct.error))

    def test_mtypes(self):
        options, _ = parse_args(self.__doc__, argv=['--test-devices',
                                                ',,audio/mp3,,audio/mpeg'])
        self.assertEqual(options['test_devices'], ['audio/mp3', 'audio/mpeg'])

    def test_same_mtypes(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--test-devices',
                                                    'audio/mp3, audio/mp3'])
        self.assertEqual(cm.exception.args[0], 2)

    def test_invalid_mtypes(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--test-devices',
                                                          'foo/mp3'])
        self.assertEqual(cm.exception.args[0], 2)

    def test_two_dumps(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--dump-default',
                                                          '--dump-internal'])
        self.assertEqual(cm.exception.args[0], 2)

    def test_log_options(self):
        with mock.patch('pa_dlna.init.setup_logging') as setup_logging:
            options, _ = parse_args(self.__doc__, argv=['--nolog-upnp',
                                                        '--log-aio'])
        self.assertEqual(options['nolog_upnp'], True)
        self.assertEqual(options['log_aio'], True)
        setup_logging.assert_called_once()

    def test_logfile(self):
        with mock.patch('builtins.open', mock.mock_open()) as m:
            options, logfile_hdler = parse_args(
                self.__doc__, argv=['--logfile', '/dummy/file/name'])
        m.assert_called_once()
        self.assertEqual(logfile_hdler.level, logging.DEBUG)

    def test_failed_logfile(self):
        error_msg = 'Test cannot open logfile'
        with mock.patch('builtins.open', mock.mock_open()) as m_open,\
                self.assertLogs(level=logging.ERROR) as m_logs,\
                self.assertRaises(SystemExit) as cm:
            m_open.side_effect = OSError(error_msg)
            options, logfile_hdler = parse_args(
                self.__doc__, argv=['--logfile', '/dummy/file/name'])

        self.assertEqual(cm.exception.args[0], 2)
        m_open.assert_called_once()
        self.assertRegex(m_logs.output[-1], f'OSError.*{error_msg}')

@requires_resources('os.devnull')
class Main(BaseTestCase):
    """padlna_main() tests."""

    def test_main(self):
        clazz = mock.MagicMock()
        coro = mock.AsyncMock()
        exit_code = 'foo'

        clazz.__name__ = 'AVControlPoint'
        app = clazz()
        app.run_control_point = coro
        coro.return_value = exit_code

        with mock.patch('pa_dlna.init.UserConfig') as cfg,\
                self.assertLogs() as logs,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['pa-dlna'])

        self.assertEqual(cm.exception.args[0], exit_code)
        cfg.assert_called_once()
        self.assertEqual(f'INFO:init:End of {app}', logs.output[-1])
        app.run_control_point.assert_called_once()
        coro.assert_awaited()

    def test_PermissionError(self):
        clazz = mock.MagicMock()
        clazz.__name__ = 'AVControlPoint'

        with self.assertLogs() as logs,\
                mock.patch('builtins.open', mock.mock_open()) as m_open,\
                self.assertRaises(SystemExit) as cm:
            m_open.side_effect = PermissionError()
            padlna_main(clazz, self.__doc__, argv=['pa-dlna'])

        self.assertEqual(cm.exception.args[0], 1)
        self.assertEqual(logs.output[-1], 'ERROR:init:PermissionError()')

    def test_upnp_cmd(self):
        clazz = mock.MagicMock()
        coro = mock.AsyncMock()
        exit_code = 'foo'

        clazz.__name__ = 'UPnPControlCmd'
        app = clazz()
        app.run_control_point = coro
        app.run.return_value = exit_code

        with self.assertLogs() as logs,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['upnp-cmd'])

        self.assertEqual(cm.exception.args[0], exit_code)
        self.assertEqual(f'INFO:init:End of {app}', logs.output[-1])
        app.run_control_point.assert_called_once()
        app.run.assert_called_once()

    def test_upnpapplication(self):
        app = UPnPApplication(logfile='foo')
        self.assertEqual(app.logfile, 'foo')

    def test_defaultconfig(self):
        clazz = mock.MagicMock()
        clazz.__name__ = 'AVControlPoint'

        with redirect_stdout(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['pa-dlna',
                                                   '--dump-default'])
        self.assertEqual(cm.exception.args[0], 0)
        doc = '# ' + Encoder.__doc__.split('\n')[0]
        self.assertEqual(output.getvalue().split('\n')[0], doc)

    def test_internalconfig(self):
        pa_dlna_conf = """
        [DEFAULT]
        selection = L16Encoder
        """
        clazz = mock.MagicMock()
        clazz.__name__ = 'AVControlPoint'

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)) as conf,\
                redirect_stdout(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['pa-dlna',
                                                   '--dump-internal'])

        self.assertEqual(cm.exception.args[0], 0)
        conf.assert_called_once_with(user_config_pathname())
        self.assertIn("'L16Encoder': {'_mime_types': ['audio/l16']",
                      output.getvalue())

if __name__ == '__main__':
    unittest.main(verbosity=2)
