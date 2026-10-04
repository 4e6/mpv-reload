#!/usr/bin/env python3
"""Fetch one mpv-reload issue or PR (library for intake.py).

    python3 .claude/tools/fetch_item.py 27     # prints the trusted metadata only

The item's text goes to the quarantined model (see intake.py) and is NOT printed.
Standard output carries only values that are safe to put in the privileged
agent's context: the kind, GitHub's own metadata (author association,
state), a login that matched GitHub's character set, the names of changed files
that ALREADY EXIST in this repository (anything else is only counted), and check
counts. Titles, bodies, comments, diffs and new file names never appear on stdout.
"""
import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("MPV_RELOAD_REPO", "4e6/mpv-reload")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
STATES = {"OPEN", "CLOSED", "MERGED"}
ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR",
                "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN"}
MAX_BODY = 20000
MAX_COMMENTS = 30
MAX_FILES_LISTED = 20

# `gh issue view` and `gh pr view` do not offer authorAssociation; the REST issue
# endpoint does, and also says whether the number is a PR.
ISSUE_FIELDS = "number,title,body,author,state,labels,createdAt,comments"
PR_FIELDS = ISSUE_FIELDS + ",files,isCrossRepository,statusCheckRollup"


def parse_number(argv):
    if len(argv) != 1 or not re.fullmatch(r"[0-9]{1,6}", argv[0]):
        raise SystemExit("usage: fetch_item.py <issue-or-pr-number>")
    return int(argv[0])


def run_gh(args):
    """Run gh and return parsed JSON; on failure exit with gh's own message."""
    try:
        out = subprocess.run(["gh"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
    except OSError as e:
        raise SystemExit("cannot run gh (is the GitHub CLI installed?): %s" % e.strerror)
    if out.returncode != 0:
        message = (out.stderr.strip().splitlines() or ["unknown error"])[0][:200]
        raise SystemExit("gh %s failed: %s" % (" ".join(args[:2]), message))
    return json.loads(out.stdout)


def clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def check_counts(rollup):
    counts = {"pass": 0, "fail": 0, "pending": 0}
    for check in rollup or []:
        state = (check.get("conclusion") or check.get("state") or "").upper()
        if state in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            counts["pass"] += 1
        elif state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            counts["fail"] += 1
        else:
            counts["pending"] += 1
    return counts


def repo_files():
    """Paths tracked in this checkout: the only file names it is safe to print."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run(["git", "-C", root, "ls-files"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, universal_newlines=True)
    return set(out.stdout.splitlines())


def classify_files(names, known):
    """Split a PR's changed files into (existing names to print, summary flags)."""
    existing = sorted(n for n in names if n in known)
    new = [n for n in names if n not in known]
    flags = {
        "NEW_FILES": len(new),
        "TOUCHES_CLAUDE_DIR": "yes" if any(n.startswith(".claude/") for n in names) else "no",
        "TOUCHES_GITHUB_DIR": "yes" if any(n.startswith(".github/") for n in names) else "no",
    }
    return existing[:MAX_FILES_LISTED], len(existing) - min(len(existing), MAX_FILES_LISTED), flags


def normalize(raw, kind, known_files=None):
    """Return (item for the quarantined model, trusted summary lines for stdout)."""
    author = (raw.get("author") or {}).get("login") or ""
    association = raw.get("authorAssociation") or "NONE"
    state = str(raw.get("state") or "?").upper()
    item = {
        "kind": kind,
        "number": raw["number"],
        "title": raw.get("title") or "",
        "body": clip(raw.get("body"), MAX_BODY),
        "author": author,
        "author_association": association,
        "state": raw.get("state"),
        "labels": [l.get("name") for l in raw.get("labels") or []],
        "created_at": raw.get("createdAt"),
        "comments": [
            {"author": (c.get("author") or {}).get("login") or "",
             "author_association": c.get("authorAssociation") or "NONE",
             "body": clip(c.get("body"), 4000)}
            for c in (raw.get("comments") or [])[-MAX_COMMENTS:]
        ],
    }
    lines = [
        "KIND=" + kind,
        "NUMBER=%d" % raw["number"],
        "STATE=" + (state if state in STATES else "?"),
        "ASSOC=" + (association if association in ASSOCIATIONS else "NONE"),
        "AUTHOR=" + (author if LOGIN.fullmatch(author) else "?"),
    ]
    if kind == "pr":
        names = [f.get("path") or "" for f in raw.get("files") or []]
        item["files"] = names[:200]
        existing, more, flags = classify_files(names, known_files if known_files is not None else repo_files())
        counts = check_counts(raw.get("statusCheckRollup"))
        lines += [
            "FROM_FORK=" + ("yes" if raw.get("isCrossRepository") else "no"),
            "EXISTING_FILES_CHANGED=" + ",".join(existing) + (" (+%d more)" % more if more else ""),
        ] + ["%s=%s" % kv for kv in sorted(flags.items())] + [
            "CHECKS=pass:%(pass)d,fail:%(fail)d,pending:%(pending)d" % counts,
        ]
    return item, lines


def fetch(number):
    meta = run_gh(["api", "repos/%s/issues/%d" % (REPO, number)])
    kind = "pr" if meta.get("pull_request") else "issue"
    fields = PR_FIELDS if kind == "pr" else ISSUE_FIELDS
    raw = run_gh([kind, "view", str(number), "-R", REPO, "--json", fields])
    raw["authorAssociation"] = meta.get("author_association") or "NONE"
    return raw, kind


def main(argv):
    """Print the trusted metadata only. intake.py is the real entry point."""
    number = parse_number(argv)
    raw, kind = fetch(number)
    _, lines = normalize(raw, kind)
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1:])
