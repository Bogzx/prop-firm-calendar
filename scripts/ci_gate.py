#!/usr/bin/env python3
"""Has CI passed for this commit? The gate scripts/autodeploy.sh asks before deploying.

    ci_gate.py OWNER/REPO SHA

Exit status: 0 = the workflow's latest run for SHA succeeded, 10 = not finished
(or not started) yet — ask again next tick, 11 = it finished without
succeeding, 2 = the question could not be answered (network, rate limit, bad
arguments). Only 0 means "deploy".

Standard library only: this runs on the VPS host, outside the container, where
nothing but python3 can be assumed. Public repositories need no token; set
GITHUB_TOKEN to lift the 60-requests-an-hour anonymous limit. GITHUB_API_URL
and AUTODEPLOY_CI_WORKFLOW override the API root and the workflow name.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

PASSED, PENDING, FAILED, UNKNOWN = 0, 10, 11, 2

_SLUG = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/*$")


def repo_slug(remote_url: str) -> str | None:
    """OWNER/REPO from an https or ssh GitHub remote, else None."""
    m = _SLUG.search(remote_url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def verdict(payload: dict, workflow: str) -> tuple[int, str]:
    """Judge an /actions/runs?head_sha= response. The newest run decides.

    A re-run of a failed workflow produces a newer run for the same commit, so
    the newest one is the answer — not "any run ever succeeded".
    """
    runs = [r for r in payload.get("workflow_runs") or [] if r.get("name") == workflow]
    if not runs:
        return PENDING, f"no '{workflow}' run for this commit yet"
    latest = max(runs, key=lambda r: (r.get("run_number") or 0, r.get("run_attempt") or 0))
    where = latest.get("html_url") or ""
    if latest.get("status") != "completed":
        return PENDING, f"'{workflow}' is {latest.get('status')} {where}".rstrip()
    if latest.get("conclusion") == "success":
        return PASSED, f"'{workflow}' passed {where}".rstrip()
    return FAILED, f"'{workflow}' concluded {latest.get('conclusion')} {where}".rstrip()


def fetch_runs(repo: str, sha: str) -> dict:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = urllib.request.Request(
        f"{api}/repos/{repo}/actions/runs?head_sha={sha}&per_page=50",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "prop-firm-calendar"},
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # A renamed repository answers with a redirect, which urllib follows.
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed scheme
        return json.load(response)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not re.fullmatch(r"[0-9a-f]{7,40}", argv[1]):
        print("usage: ci_gate.py OWNER/REPO SHA", file=sys.stderr)
        return UNKNOWN
    repo, sha = argv
    workflow = os.environ.get("AUTODEPLOY_CI_WORKFLOW", "CI")
    try:
        payload = fetch_runs(repo, sha)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"could not ask GitHub about {repo}@{sha[:7]}: {e}", file=sys.stderr)
        return UNKNOWN
    code, message = verdict(payload, workflow)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
