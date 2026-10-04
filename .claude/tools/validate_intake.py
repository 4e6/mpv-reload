#!/usr/bin/env python3
"""Validate what the `mpv-intake` subagent returned, then print a normalized copy.

    python3 .claude/tools/validate_intake.py <dir>/intake.json

The privileged agent acts only on the printed copy, which is rebuilt from the
validated fields. Exit status 1 and a message on stderr for anything off-schema: unknown or missing
keys, values outside their enum, a version that is not a plain number, over-long
text, or anything but printable ASCII. Error messages never repeat the input, only
counts and the schema's own names: they are read by the privileged agent.
"""
import json
import re
import sys

KINDS = {"bug", "compat", "feature", "question", "duplicate", "spam", "pr"}
SYSTEMS = {"linux", "macos", "windows", "bsd", "other", "unknown"}
VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}(\.[0-9]{1,3})?")
KEYS = {"kind", "mpv_version", "os", "has_debug_log", "has_repro",
        "duplicate_of", "injection_suspected", "injection_reason", "summary"}
MAX_REASON = 200
MAX_SUMMARY = 300
MAX_DUPLICATES = 5


class Invalid(Exception):
    pass


def text(value, name, limit, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise Invalid("%s must be a string" % name)
    if len(value) > limit:
        raise Invalid("%s is longer than %d characters" % (name, limit))
    if any(not 32 <= ord(c) < 127 for c in value):
        raise Invalid("%s must be one line of printable ASCII" % name)
    return value


def validate(data):
    if not isinstance(data, dict):
        raise Invalid("intake output must be a JSON object")
    extra, missing = set(data) - KEYS, KEYS - set(data)
    if extra or missing:
        raise Invalid("keys differ from the schema (%d unexpected, missing: %s)"
                      % (len(extra), sorted(missing)))  # never echo attacker-chosen key names
    if data["kind"] not in KINDS:
        raise Invalid("kind must be one of %s" % sorted(KINDS))
    version = data["mpv_version"]
    if version is not None and not (isinstance(version, str) and VERSION.fullmatch(version)):
        raise Invalid("mpv_version must be null or look like 0.37 or 0.37.0")
    if data["os"] not in SYSTEMS:
        raise Invalid("os must be one of %s" % sorted(SYSTEMS))
    for name in ("has_debug_log", "has_repro", "injection_suspected"):
        if not isinstance(data[name], bool):
            raise Invalid("%s must be true or false" % name)
    dups = data["duplicate_of"]
    if (not isinstance(dups, list) or len(dups) > MAX_DUPLICATES
            or not all(isinstance(n, int) and not isinstance(n, bool) and 0 < n < 10**6 for n in dups)):
        raise Invalid("duplicate_of must be a list of at most %d issue numbers" % MAX_DUPLICATES)
    reason = text(data["injection_reason"], "injection_reason", MAX_REASON, allow_none=True)
    if data["injection_suspected"] and not reason:
        raise Invalid("injection_suspected needs an injection_reason")
    if reason and not data["injection_suspected"]:
        raise Invalid("injection_reason is only allowed when injection_suspected is true")
    summary = text(data["summary"], "summary", MAX_SUMMARY)
    if not summary.strip():
        raise Invalid("summary must not be empty")
    return {
        "kind": data["kind"], "mpv_version": version, "os": data["os"],
        "has_debug_log": data["has_debug_log"], "has_repro": data["has_repro"],
        "duplicate_of": sorted(set(dups)),
        "injection_suspected": data["injection_suspected"],
        "injection_reason": reason, "summary": summary,
    }


def main(argv):
    if len(argv) != 1:
        raise SystemExit("usage: validate_intake.py <intake.json>")
    try:
        with open(argv[0]) as f:
            clean = validate(json.load(f))
    except (OSError, ValueError, RecursionError, Invalid) as e:
        if not isinstance(e, Invalid):
            e = "not valid JSON" if not isinstance(e, OSError) else "cannot read the file"
        sys.stderr.write("INVALID INTAKE: %s\n" % e)
        return 1
    print(json.dumps(clean, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
