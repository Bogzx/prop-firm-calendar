"""The production server deploys main every five minutes; only after CI passes.

scripts/autodeploy.sh runs on the VPS host from a systemd timer, so these
tests drive the real script against a throwaway origin, a stub `docker` and a
local stand-in for the GitHub API.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

_spec = importlib.util.spec_from_file_location("ci_gate", SCRIPTS / "ci_gate.py")
assert _spec is not None and _spec.loader is not None
ci_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_gate)


def run(number: int, status: str, conclusion: str | None, name: str = "CI") -> dict:
    return {"name": name, "run_number": number, "status": status, "conclusion": conclusion}


# -- the verdict ------------------------------------------------------------


def test_a_successful_run_passes() -> None:
    code, _ = ci_gate.verdict({"workflow_runs": [run(1, "completed", "success")]}, "CI")
    assert code == ci_gate.PASSED


def test_a_running_workflow_means_wait() -> None:
    code, message = ci_gate.verdict({"workflow_runs": [run(1, "in_progress", None)]}, "CI")
    assert code == ci_gate.PENDING and "in_progress" in message


def test_no_run_yet_means_wait() -> None:
    assert ci_gate.verdict({"workflow_runs": []}, "CI")[0] == ci_gate.PENDING


def test_a_failure_blocks() -> None:
    code, _ = ci_gate.verdict({"workflow_runs": [run(1, "completed", "failure")]}, "CI")
    assert code == ci_gate.FAILED


def test_the_newest_run_decides() -> None:
    """A green re-run after a red one unblocks; an old green does not mask a new red."""
    rerun_green = [run(1, "completed", "failure"), run(2, "completed", "success")]
    assert ci_gate.verdict({"workflow_runs": rerun_green}, "CI")[0] == ci_gate.PASSED
    new_red = [run(1, "completed", "success"), run(2, "completed", "failure")]
    assert ci_gate.verdict({"workflow_runs": new_red}, "CI")[0] == ci_gate.FAILED


def test_other_workflows_are_ignored() -> None:
    runs = [run(5, "completed", "success", name="Pages"), run(1, "completed", "failure")]
    assert ci_gate.verdict({"workflow_runs": runs}, "CI")[0] == ci_gate.FAILED


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("https://github.com/Bogzx/prop-firm-calendar.git", "Bogzx/prop-firm-calendar"),
        ("https://github.com/Bogzx/ftmo-calendar", "Bogzx/ftmo-calendar"),
        ("git@github.com:Bogzx/prop-firm-calendar.git", "Bogzx/prop-firm-calendar"),
        ("ssh://git@github.com/Bogzx/prop-firm-calendar.git/", "Bogzx/prop-firm-calendar"),
        ("/srv/git/calendar.git", None),
    ],
)
def test_repo_slug(remote: str, slug: str | None) -> None:
    assert ci_gate.repo_slug(remote) == slug


def test_a_bad_sha_is_refused() -> None:
    assert ci_gate.main(["Bogzx/prop-firm-calendar", "main; rm -rf /"]) == ci_gate.UNKNOWN


# -- the script, end to end -------------------------------------------------

needs_tools = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git"
)


class FakeGitHub:
    """Answers /repos/.../actions/runs with whatever `runs` currently holds."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.requests: list[str] = []
        self.auth: list[str | None] = []
        #: (status, raw body) to answer with instead of `runs`.
        self.answer: tuple[int, bytes] | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(self.path)
                outer.auth.append(self.headers.get("Authorization"))
                listing = json.dumps({"workflow_runs": outer.runs}).encode()
                code, body = outer.answer or (200, listing)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # noqa: ANN002
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def deploy(tmp_path: Path):
    """A deploy clone one commit behind its origin, with docker stubbed out."""

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    for name in ("autodeploy.sh", "ci_gate.py"):
        shutil.copy2(SCRIPTS / name, work / "scripts" / name)
    # Not `init -b main`: that needs git 2.28, and the production host's git
    # (2.25) runs this script. symbolic-ref names the branch on any git.
    git(tmp_path, "init", "-q", str(work))
    git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "one")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(work, "remote", "add", "origin", str(origin))
    (work / "new.txt").write_text("new", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "two")
    git(work, "push", "-q", "origin", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    stub = bin_dir / "docker"
    stub.write_text(
        f'#!/bin/sh\necho "$@" >> "{docker_log}"\n[ -z "$DOCKER_FAIL" ]\n', encoding="utf-8"
    )
    stub.chmod(0o755)

    github = FakeGitHub()

    def invoke(**env: str) -> subprocess.CompletedProcess:
        # A developer's real token must never reach the fake API.
        inherited = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        full_env = {
            **inherited,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_API_URL": github.url,
            "AUTODEPLOY_REPO": "Bogzx/prop-firm-calendar",
            **env,
        }
        return subprocess.run(
            ["bash", str(clone / "scripts" / "autodeploy.sh")],
            env=full_env,
            capture_output=True,
            text=True,
        )

    def head() -> str:
        return git(clone, "rev-parse", "HEAD")

    target = git(work, "rev-parse", "HEAD")
    yield invoke, github, head, target, docker_log
    github.httpd.shutdown()
    github.httpd.server_close()


@needs_tools
def test_it_waits_while_ci_is_running(deploy) -> None:
    invoke, github, head, target, docker_log = deploy
    github.runs = [run(1, "in_progress", None)]
    result = invoke()
    assert result.returncode == 0, result.stderr
    assert "waiting for CI" in result.stdout
    assert head() != target
    assert not docker_log.exists()
    assert f"head_sha={target}" in github.requests[0]


@needs_tools
def test_it_refuses_a_red_commit(deploy) -> None:
    invoke, github, head, target, docker_log = deploy
    github.runs = [run(1, "completed", "failure")]
    result = invoke()
    assert result.returncode == 1
    assert "not deploying" in result.stderr
    assert head() != target
    assert not docker_log.exists()


@needs_tools
def test_it_deploys_a_green_commit(deploy) -> None:
    invoke, github, head, target, docker_log = deploy
    github.runs = [run(1, "completed", "success")]
    result = invoke()
    assert result.returncode == 0, result.stderr
    assert head() == target
    assert docker_log.read_text(encoding="utf-8").strip() == "compose up -d --build"


@needs_tools
def test_the_gate_can_be_turned_off(deploy) -> None:
    invoke, github, head, target, docker_log = deploy
    github.runs = [run(1, "completed", "failure")]
    result = invoke(AUTODEPLOY_REQUIRE_CI="0")
    assert result.returncode == 0, result.stderr
    assert head() == target
    assert github.requests == []


@needs_tools
def test_an_unreachable_api_does_not_deploy(deploy) -> None:
    invoke, _github, head, target, docker_log = deploy
    result = invoke(GITHUB_API_URL="http://127.0.0.1:9")
    assert result.returncode == 1
    assert "CI status unknown" in result.stderr
    assert head() != target


@needs_tools
def test_a_non_github_origin_needs_an_explicit_repo(deploy) -> None:
    invoke, _github, head, target, _log = deploy
    result = invoke(AUTODEPLOY_REPO="")
    assert result.returncode == 1
    assert "set AUTODEPLOY_REPO" in result.stderr
    assert head() != target


@needs_tools
@pytest.mark.parametrize(
    ("code", "body"),
    [
        (403, b'{"message": "API rate limit exceeded for 203.0.113.9."}'),
        (500, b"oops"),
        (200, b"<html>captive portal</html>"),
    ],
)
def test_an_unusable_api_answer_does_not_deploy(deploy, code: int, body: bytes) -> None:
    """Rate limited, erroring or not JSON: the old version keeps running."""
    invoke, github, head, target, docker_log = deploy
    github.answer = (code, body)
    result = invoke()
    assert result.returncode == 1
    assert "CI status unknown" in result.stderr
    assert head() != target
    assert not docker_log.exists()


@needs_tools
def test_the_token_is_sent_when_set_and_not_otherwise(deploy) -> None:
    invoke, github, _head, _target, _log = deploy
    github.runs = [run(1, "in_progress", None)]
    invoke()
    invoke(GITHUB_TOKEN="t0ken")
    assert github.auth == [None, "Bearer t0ken"]


@needs_tools
def test_a_failed_build_is_retried_next_tick(deploy) -> None:
    """Review: HEAD already matched origin after a failed build, so no retry ever came."""
    invoke, github, head, target, docker_log = deploy
    github.runs = [run(1, "completed", "success")]
    before = head()
    result = invoke(DOCKER_FAIL="1")
    assert result.returncode == 1
    assert "will retry next tick" in result.stderr
    assert head() == before
    # The old version was asked back up after the failed attempt.
    assert docker_log.read_text(encoding="utf-8").splitlines() == ["compose up -d --build"] * 2

    result = invoke()
    assert result.returncode == 0, result.stderr
    assert head() == target
