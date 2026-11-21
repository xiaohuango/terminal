"""Container-side runner. A missing/invalid task artifact is an assertion failure.

Other exceptions (including imports, permissions and broken tests) remain errors.
"""
import io
import json
import sys
from pathlib import Path
import unittest

import test_state


original_observe = test_state.observe


def observe(check):
    try:
        return original_observe(check)
    except (FileNotFoundError, NotADirectoryError, UnicodeError,
            json.JSONDecodeError, KeyError, IndexError) as error:
        raise AssertionError(f"Required artifact {check['path']}: {error}") from error


test_state.observe = observe


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def record(self, test, status, detail=""):
        self.rows.append({"id": test._testMethodName.removeprefix("test_"),
                          "status": status, "detail": detail})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.record(test, "failed", self._exc_info_to_string(error, test))

    def addError(self, test, error):
        super().addError(test, error)
        self.record(test, "error", self._exc_info_to_string(error, test))


suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_state.StateTests)
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, resultclass=Result, verbosity=2).run(suite)
Path('/logs/verifier').mkdir(parents=True, exist_ok=True)
Path('/logs/verifier/reward.txt').write_text('1\n' if result.wasSuccessful() else '0\n')
print(json.dumps({"assertions": result.rows, "tests_run": result.testsRun,
                  "transcript": stream.getvalue()}))
sys.exit(2 if result.errors else 0 if result.wasSuccessful() else 1)
