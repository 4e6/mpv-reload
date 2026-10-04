#!/usr/bin/env python3
"""Describe one mpv-reload issue or PR without letting its text reach the caller.

    python3 .claude/tools/intake.py 27

1. fetch_item fetches the item from GitHub (nothing is written to disk).
2. The item is sent on stdin to a separate `claude -p` process that has NO tools
   (`--tools ""`), no MCP servers, no saved session, and runs in an empty temp
   directory, so it cannot read, write, fetch or run anything. Its system prompt
   is intake_prompt.md.
3. Its reply is parsed and checked by validate_intake. The raw reply is never
   printed, whatever happens.
4. Standard output is only: trusted metadata from fetch_item, then the validated,
   normalized fields as one JSON line ("INTAKE=..."). On any problem it prints
   "INTAKE_FAILED=<fixed reason>" and exits 1.

The privileged agent that runs this sees nothing the quarantined model wrote except
fields that passed the schema. A compromised reply can still choose values inside the
schema (an enum, a version number, a 300-character ASCII sentence); nothing more.

Tests can set MPV_RELOAD_INTAKE_FIXTURE=<item json file> to skip GitHub, and
MPV_RELOAD_CLAUDE to replace the claude command.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_item  # noqa: E402
import validate_intake  # noqa: E402

PROMPT_FILE = os.path.join(HERE, "intake_prompt.md")
TIMEOUT = 180
MAX_REPLY = 6000
FENCE = re.compile(r"\A```(?:json)?\n(.*)\n```\Z", re.S)


class Failed(Exception):
    """Carries a fixed reason; never any model or issue text."""


def claude_command():
    return os.environ.get("MPV_RELOAD_CLAUDE", "claude").split()


def run_model(item, command=None):
    """Send `item` to the tool-less model; return the reply text (untrusted)."""
    with open(PROMPT_FILE) as f:
        system_prompt = f.read()
    work = tempfile.mkdtemp(prefix="mpv-intake-")  # empty cwd: no project files or settings
    try:
        argv = (command or claude_command()) + [
            "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
            "--model", "sonnet", "--output-format", "json",
            "--system-prompt", system_prompt,
        ]
        try:
            done = subprocess.run(
                argv, input=json.dumps(item), cwd=work, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, universal_newlines=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Failed("the intake model timed out")
        except OSError:
            raise Failed("cannot run the claude command")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if done.returncode != 0:
        raise Failed("the intake model exited with an error")
    try:
        outer = json.loads(done.stdout)
        reply = outer["result"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise Failed("the intake model returned an unexpected envelope")
    if outer.get("is_error") or not isinstance(reply, str):
        raise Failed("the intake model reported an error")
    return reply


def parse_reply(reply):
    """Untrusted reply text -> validated fields. Raises Failed with a fixed reason."""
    if len(reply) > MAX_REPLY:
        raise Failed("the intake reply was too long")
    text = reply.strip()
    fenced = FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise Failed("the intake reply was not a JSON object")
    try:
        return validate_intake.validate(data)
    except validate_intake.Invalid as e:
        raise Failed("the intake reply broke the schema: %s" % e)


def main(argv):
    number = fetch_item.parse_number(argv)
    fixture = os.environ.get("MPV_RELOAD_INTAKE_FIXTURE")
    if fixture:
        with open(fixture) as f:
            item = json.load(f)
        kind = item.get("kind", "issue")
        lines = ["KIND=" + str(kind)[:5].replace("\n", " "), "NUMBER=%d" % number, "FIXTURE=yes"]
    else:
        raw, kind = fetch_item.fetch(number)
        item, lines = fetch_item.normalize(raw, kind)
    print("\n".join(lines))
    try:
        fields = parse_reply(run_model(item))
    except Failed as e:
        print("INTAKE_FAILED=%s" % e)
        return 1
    print("INTAKE=" + json.dumps(fields, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
