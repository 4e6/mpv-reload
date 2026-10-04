#!/usr/bin/env python3
"""Fetch one mpv-reload issue or PR into a private temp dir.

    python3 .claude/tools/fetch_item.py 27

The item's text is written to <dir>/item.json for the `mpv-intake` subagent and
is NOT printed. Standard output carries only values that are safe to put in the
privileged agent's context: the temp dir, the kind, GitHub's own metadata
(author association, state), a login that matched GitHub's character set, the
changed file names that matched a conservative pattern, and check counts.
Titles, bodies, comments and diffs never appear on stdout.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.environ.get("MPV_RELOAD_REPO", "4e6/mpv-reload")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
PATH = re.compile(r"[A-Za-z0-9._/-]{1,120}")
ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR",
                "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN"}
MAX_BODY = 20000
MAX_COMMENTS = 30

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


def normalize(raw, kind):
    """Return (item for the subagent, trusted summary lines for stdout)."""
    author = (raw.get("author") or {}).get("login") or ""
    association = raw.get("authorAssociation") or "NONE"
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
        "STATE=" + str(raw.get("state") or "?").upper(),
        "ASSOC=" + (association if association in ASSOCIATIONS else "NONE"),
        "AUTHOR=" + (author if LOGIN.fullmatch(author) else "?"),
    ]
    if kind == "pr":
        names = [f.get("path") or "" for f in raw.get("files") or []]
        good = [n for n in names if PATH.fullmatch(n)]
        item["files"] = good
        item["files_with_odd_names"] = len(names) - len(good)
        counts = check_counts(raw.get("statusCheckRollup"))
        lines += [
            "FROM_FORK=" + ("yes" if raw.get("isCrossRepository") else "no"),
            "FILES=" + ",".join(good),
            "ODD_FILE_NAMES=%d" % (len(names) - len(good)),
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
    number = parse_number(argv)
    raw, kind = fetch(number)
    item, lines = normalize(raw, kind)
    directory = tempfile.mkdtemp(prefix="mpv-intake-")  # mode 0700
    with open(os.path.join(directory, "item.json"), "w") as f:
        json.dump(item, f, indent=1)
    print("DIR=" + directory)
    for line in lines:
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])
