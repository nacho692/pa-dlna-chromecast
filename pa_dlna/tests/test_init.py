"""Command line test cases."""

import struct
import logging
import unittest
from unittest import mock

from . import BaseTestCase, requires_resources
from ..init import parse_args

@requires_resources('os.devnull')
class Argv(BaseTestCase):
    """Command line tests."""

    def setUp(self):
        super().setUp()

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
        options, _ = parse_args(self.__doc__, argv=['--nolog-upnp',
                                                      '--log-aio'])
        self.assertEqual(options['nolog_upnp'], True)
        self.assertEqual(options['log_aio'], True)

    def test_logfile(self):
        with mock.patch('builtins.open', mock.mock_open()) as m:
            options, logfile_hdler = parse_args(
                self.__doc__, argv=['--logfile', '/dummy/file/name'])
        m.assert_called_once()
        self.assertEqual(logfile_hdler.level, logging.DEBUG)

    def test_failed_logfile(self):
        error_msg = 'Test cannot open logfile'
        with (mock.patch('builtins.open', mock.mock_open()) as m_open,
                self.assertLogs(level=logging.ERROR) as m_logs,
                self.assertRaises(SystemExit) as cm):
            m_open.side_effect = OSError(error_msg)
            options, logfile_hdler = parse_args(
                self.__doc__, argv=['--logfile', '/dummy/file/name'])
        m_open.assert_called_once()
        self.assertRegex(m_logs.output[-1], f'OSError.*{error_msg}')
        self.assertEqual(cm.exception.args[0], 2)

    def tearDown(self):
        super().tearDown()

@requires_resources('os.devnull')
class Main(BaseTestCase):
    """padlna_main() tests."""

    def setUp(self):
        super().setUp()

    def tearDown(self):
        super().tearDown()

if __name__ == '__main__':
    unittest.main(verbosity=2)
