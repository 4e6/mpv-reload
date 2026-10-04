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
4. Standard output is only: trusted metadata from fetch_item, a link to the item, and
   the validated fields that are enums, booleans, numbers or a version ("INTAKE=...").
   The two free-text fields the model also writes (summary, injection_reason) are NOT
   printed: the privileged agent that runs this never sees a sentence the model wrote,
   so a compromised reply can choose only among enum and boolean values. The owner can
   read the item at the link, or run this by hand with MPV_RELOAD_INTAKE_SHOW_TEXT=1 to
   see the two text fields. On any problem it prints "INTAKE_FAILED=<fixed reason>".

    python3 .claude/tools/intake.py --list      # untriaged items: number, login, date

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
TEXT_FIELDS = ("summary", "injection_reason")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z")
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
            # project-only settings from an empty cwd: none of the owner's user-level
            # hooks, plugins or skills load, and none of them is handed the item text.
            "--setting-sources", "project", "--disable-slash-commands",
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


def list_untriaged():
    """Open issues and PRs without the `triaged` label: numbers, logins and dates only."""
    rows = []
    for kind in ("issue", "pr"):
        found = fetch_item.run_gh([kind, "list", "-R", fetch_item.REPO, "--state", "open",
                                   "--search", "-label:triaged", "--limit", "50",
                                   "--json", "number,author,createdAt"])
        for row in found:
            login = (row.get("author") or {}).get("login") or ""
            created = row.get("createdAt") or ""
            rows.append((created if DATE.fullmatch(created) else "?", kind, int(row["number"]),
                         login if fetch_item.LOGIN.fullmatch(login) else "?"))
    return sorted(rows, reverse=True)


def main(argv):
    if argv == ["--list"]:
        for created, kind, number, login in list_untriaged():
            print("%s\t%s\t#%d\t%s" % (kind, created, number, login))
        return 0
    number = fetch_item.parse_number(argv)
    fixture = os.environ.get("MPV_RELOAD_INTAKE_FIXTURE")
    if fixture:
        with open(fixture) as f:
            item = json.load(f)
        lines = ["KIND=" + ("pr" if item.get("kind") == "pr" else "issue"), "NUMBER=%d" % number, "FIXTURE=yes"]
    else:
        raw, kind = fetch_item.fetch(number)
        item, lines = fetch_item.normalize(raw, kind)
    print("\n".join(lines))
    print("URL=https://github.com/%s/issues/%d" % (fetch_item.REPO, number))
    try:
        fields = parse_reply(run_model(item))
    except Failed as e:
        print("INTAKE_FAILED=%s" % e)
        return 1
    shown = {k: v for k, v in fields.items() if k not in TEXT_FIELDS}
    print("INTAKE=" + json.dumps(shown, sort_keys=True))
    if os.environ.get("MPV_RELOAD_INTAKE_SHOW_TEXT") == "1":   # the owner, by hand
        for name in TEXT_FIELDS:
            print("%s=%s" % (name.upper(), fields[name]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
