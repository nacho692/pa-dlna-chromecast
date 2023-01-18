import unittest

def load_ordered_tests(loader, standard_tests, pattern):
    """Keep the tests in the order they were declared in the class.

    Thanks to https://stackoverflow.com/a/62073640
    """

    ordered_cases = []
    for test_suite in standard_tests:
        ordered = []
        for test_case in test_suite:
            test_case_type = type(test_case)
            method_name = test_case._testMethodName
            testMethod = getattr(test_case, method_name)
            line = testMethod.__code__.co_firstlineno
            ordered.append( (line, test_case_type, method_name) )
        ordered.sort()
        for line, case_type, name in ordered:
            ordered_cases.append(case_type(name))
    return unittest.TestSuite(ordered_cases)

def find_in_logs(logs, logger, msg):
    """Return True if 'msg' from 'logger' is in 'logs'."""

    for log in (log.split(':', maxsplit=2) for log in logs):
        if len(log) == 3 and log[1] == logger and log[2] == msg:
            return True
    return False

def search_in_logs(logs, logger, matcher):
    """Return True if the matcher's pattern is found in a message in 'logs'."""

    for log in (log.split(':', maxsplit=2) for log in logs):
        if (len(log) == 3 and log[1] == logger and
                matcher.search(log[2]) is not None):
            return True
    return False
