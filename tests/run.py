#!/usr/bin/env python3
"""Run the test suite against the mpv in $MPV (default: mpv).

    python3 tests/run.py            # everything
    python3 tests/run.py -k stall   # only tests whose id contains "stall"

Exit status is 0 when every test passed or failed as listed in
known_failing.py, and 1 on any other failure or on an unexpected pass.
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mpvtest  # noqa: E402
from known_failing import KNOWN_FAILING  # noqa: E402


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for sub in flatten(item):
                yield sub
        else:
            yield item


def expected_failure(test_id, failure_text, cell):
    row = KNOWN_FAILING.get(test_id)
    if not row:
        return False
    if "*" not in row["cells"] and cell not in row["cells"]:
        return False
    # Only the final exception counts: the traceback above it quotes source
    # lines, which contain the message text even when the failure is something
    # else (for example an IPC error raised while evaluating the arguments).
    marker = failure_text.rfind("\nAssertionError")
    return marker != -1 and row["message"] in failure_text[marker:]


def listed_for(test_id, cell):
    row = KNOWN_FAILING.get(test_id)
    return bool(row) and ("*" in row["cells"] or cell in row["cells"])


def main(argv):
    pattern = None
    if "-k" in argv:
        pattern = argv[argv.index("-k") + 1]
    major, minor = mpvtest.mpv_version()
    cell = "%d.%d" % (major, minor)
    print("mpv %s via %r, script %s" % (cell, mpvtest.MPV, mpvtest.SCRIPT))

    suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    tests = [t for t in flatten(suite) if not pattern or pattern in t.id()]
    bad = 0
    for test in tests:
        started = time.time()
        result = unittest.TestResult()
        test.run(result)
        # (is_assertion_failure, text); errors (crashes, timeouts) are never expected.
        problems = [(True, t) for _, t in result.failures] + [(False, t) for _, t in result.errors]
        test_id = test.id()
        elapsed = time.time() - started
        if not problems:
            if listed_for(test_id, cell):
                print("UNEXPECTED PASS %s (%.1fs): fixed? delete its row in tests/known_failing.py" % (test_id, elapsed))
                bad += 1
            else:
                print("ok              %s (%.1fs)" % (test_id, elapsed))
        elif all(is_failure and expected_failure(test_id, text, cell) for is_failure, text in problems):
            print("expected fail   %s (%.1fs)" % (test_id, elapsed))
        else:
            print("FAIL            %s (%.1fs)" % (test_id, elapsed))
            for _, text in problems:
                print("    " + text.rstrip().replace("\n", "\n    "))
            bad += 1
    print("\n%d test(s), %d problem(s)" % (len(tests), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
