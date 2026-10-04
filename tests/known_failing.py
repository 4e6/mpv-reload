"""Tests that fail today because of a known bug in main.lua.

run.py treats a listed failure as expected so CI stays green, and treats a
listed test that PASSES as a failure: whoever fixes the bug must delete the
row in the same change, which proves the fix and keeps this list honest.

A row only counts when
  - the running mpv matches `cells` ("*" or "major.minor" strings), and
  - the failure is an AssertionError containing `message`.
Crashes and timeouts are never "expected".
"""

KNOWN_FAILING = {
}
