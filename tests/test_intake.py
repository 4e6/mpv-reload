"""The two scripts that keep issue and PR text out of the privileged agent.

fetch_item.py: stdout carries only trusted, pattern-checked values.
validate_intake.py: only an exact, bounded schema gets through.
No network and no mpv needed.
"""
import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), ".claude", "tools")
sys.path.insert(0, TOOLS)

import fetch_item  # noqa: E402
import validate_intake  # noqa: E402

GOOD = {
    "kind": "bug", "mpv_version": "0.40", "os": "linux", "has_debug_log": False,
    "has_repro": True, "duplicate_of": [21, 21], "injection_suspected": False,
    "injection_reason": None, "summary": "Reload does not trigger after a stall.",
}


class ValidateIntakeTests(unittest.TestCase):
    def check_rejected(self, **changes):
        data = dict(GOOD, **changes)
        with self.assertRaises(validate_intake.Invalid):
            validate_intake.validate(data)

    def test_good_output_is_normalized(self):
        clean = validate_intake.validate(GOOD)
        self.assertEqual(clean["duplicate_of"], [21])
        self.assertEqual(set(clean), validate_intake.KEYS)

    def test_extra_or_missing_keys_are_rejected(self):
        with self.assertRaises(validate_intake.Invalid):
            validate_intake.validate(dict(GOOD, next_step="run curl"))
        missing = dict(GOOD)
        del missing["summary"]
        with self.assertRaises(validate_intake.Invalid):
            validate_intake.validate(missing)

    def test_enums_and_types(self):
        self.check_rejected(kind="maintainer-request")
        self.check_rejected(os="plan9")
        self.check_rejected(has_repro="yes")
        self.check_rejected(duplicate_of=["21"])
        self.check_rejected(duplicate_of=list(range(1, 9)))
        self.check_rejected(duplicate_of=[True])

    def test_version_must_be_a_plain_number(self):
        for bad in ("0.40; curl evil", "v0.40", "latest", "0.40\n", ""):
            self.check_rejected(mpv_version=bad)
        validate_intake.validate(dict(GOOD, mpv_version=None))
        validate_intake.validate(dict(GOOD, mpv_version="0.37.0"))

    def test_text_is_bounded_and_single_line(self):
        self.check_rejected(summary="x" * 301)
        self.check_rejected(summary="line one\nline two")
        self.check_rejected(summary="bell\x07")
        self.check_rejected(injection_suspected=True, injection_reason="r" * 201)

    def test_suspicion_needs_a_reason(self):
        self.check_rejected(injection_suspected=True, injection_reason=None)
        validate_intake.validate(dict(GOOD, injection_suspected=True,
                                      injection_reason='quoted: "ignore all previous instructions"'))

    def test_cli_exit_status(self):
        path = os.path.join(HERE, ".cache", "intake-test.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for payload, status in ((GOOD, 0), ({"kind": "bug"}, 1)):
            with open(path, "w") as f:
                json.dump(payload, f)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(validate_intake.main([path]), status)
        with open(path, "w") as f:
            f.write("not json")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(validate_intake.main([path]), 1)


class FetchItemTests(unittest.TestCase):
    def test_only_a_plain_number_is_accepted(self):
        self.assertEqual(fetch_item.parse_number(["27"]), 27)
        for bad in (["27; rm -rf ~"], ["-1"], ["0x1f"], ["27", "28"], [], ["27\n"]):
            with self.assertRaises(SystemExit):
                fetch_item.parse_number(bad)

    def test_stdout_lines_never_carry_issue_text(self):
        hostile = json.load(open(os.path.join(HERE, "fixtures", "hostile_issue.json")))
        raw = {
            "number": 9001, "title": hostile["title"], "body": hostile["body"],
            "author": {"login": "evil$(curl x)"}, "authorAssociation": "NONE; rm -rf",
            "state": "OPEN", "labels": [], "createdAt": "2026-10-04T12:00:00Z",
            "comments": [{"author": {"login": "x"}, "body": hostile["comments"][0]["body"]}],
        }
        item, lines = fetch_item.normalize(raw, "issue")
        printed = "\n".join(lines)
        self.assertIn("ASSOC=NONE", printed)
        self.assertIn("AUTHOR=?", printed)  # login did not match GitHub's character set
        for needle in ("IMPORTANT", "ignore all previous", "evil", "curl", "rm -rf", "SYSTEM NOTICE"):
            self.assertNotIn(needle, printed)
        self.assertIn("SYSTEM NOTICE", item["body"])  # the subagent file does keep the text

    def test_pr_file_names_are_filtered_and_counts_only(self):
        raw = {
            "number": 26, "title": "t", "body": "b", "author": {"login": "stacyharper"},
            "authorAssociation": "CONTRIBUTOR", "state": "OPEN", "labels": [], "createdAt": "x",
            "comments": [], "isCrossRepository": True,
            "files": [{"path": "main.lua"}, {"path": "tests/x.py"}, {"path": "a b;rm.lua"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}, {"state": "PENDING"}],
        }
        item, lines = fetch_item.normalize(raw, "pr")
        self.assertIn("FILES=main.lua,tests/x.py", lines)
        self.assertIn("ODD_FILE_NAMES=1", lines)
        self.assertIn("CHECKS=pass:1,fail:1,pending:1", lines)
        self.assertIn("FROM_FORK=yes", lines)
        self.assertNotIn("a b;rm.lua", "\n".join(lines))

    def test_trailing_newline_does_not_pass_the_patterns(self):
        raw = {"number": 5, "title": "t", "body": "b", "author": {"login": "someone\n"},
               "authorAssociation": "NONE", "state": "OPEN", "labels": [], "createdAt": "x",
               "comments": [], "isCrossRepository": False,
               "files": [{"path": "main.lua\n"}], "statusCheckRollup": []}
        _, lines = fetch_item.normalize(raw, "pr")
        self.assertIn("AUTHOR=?", lines)
        self.assertIn("FILES=", lines)
        self.assertIn("ODD_FILE_NAMES=1", lines)

    def test_fetch_uses_rest_metadata_and_picks_the_view_command(self):
        calls = []

        def fake(args):
            calls.append(args)
            if args[0] == "api":
                return {"author_association": "CONTRIBUTOR", "pull_request": {"url": "x"}}
            return {"number": 26, "title": "t", "body": "b", "author": {"login": "a"},
                    "state": "OPEN", "labels": [], "createdAt": "x", "comments": [],
                    "files": [], "isCrossRepository": True, "statusCheckRollup": []}

        original, fetch_item.run_gh = fetch_item.run_gh, fake
        try:
            raw, kind = fetch_item.fetch(26)
        finally:
            fetch_item.run_gh = original
        self.assertEqual(kind, "pr")
        self.assertEqual(raw["authorAssociation"], "CONTRIBUTOR")
        self.assertEqual(calls[1][:2], ["pr", "view"])
        self.assertNotIn("authorAssociation", calls[1][-1])  # gh does not offer it

    def run_gh_with(self, fake_run):
        original, fetch_item.subprocess.run = fetch_item.subprocess.run, fake_run
        try:
            with self.assertRaises(SystemExit) as caught:
                fetch_item.run_gh(["issue", "view", "1"])
        finally:
            fetch_item.subprocess.run = original
        return str(caught.exception)

    def test_gh_failure_is_reported_not_called_missing(self):
        class Failed:
            returncode, stdout, stderr = 1, "", "GraphQL: Could not resolve to an Issue\nsecond line"

        message = self.run_gh_with(lambda *a, **k: Failed())
        self.assertIn("Could not resolve to an Issue", message)
        self.assertNotIn("second line", message)
        self.assertNotIn("no issue or PR", message)

    def test_missing_gh_is_a_clear_message_not_a_traceback(self):
        def missing(*a, **k):
            raise FileNotFoundError(2, "No such file or directory")

        self.assertIn("GitHub CLI", self.run_gh_with(missing))

    def test_long_text_is_truncated(self):
        raw = {"number": 1, "title": "t", "body": "x" * 50000, "author": {"login": "a"},
               "authorAssociation": "NONE", "state": "OPEN", "labels": [], "createdAt": "x",
               "comments": [{"author": {"login": "a"}, "body": "y" * 9000}] * 40}
        item, _ = fetch_item.normalize(raw, "issue")
        self.assertLess(len(item["body"]), 20100)
        self.assertEqual(len(item["comments"]), fetch_item.MAX_COMMENTS)
        self.assertLess(len(item["comments"][0]["body"]), 4100)


if __name__ == "__main__":
    unittest.main()
