"""The files a self-hoster actually uses must agree with the code.

Every problem pinned here was real: config.example.toml documented a taxonomy
two releases out of date, and compose.yaml shipped one maintainer's reverse
proxy arrangement as everyone's default, publishing the feed on a loopback
address and a port the README never mentions. Docs drift silently; tests do not.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ftmo_calendar.config import DEFAULT_SUMMARIES, ServeConfig, load_config

REPO = Path(__file__).parent.parent
CONFIG_EXAMPLE = REPO / "config.example.toml"
COMPOSE = REPO / "compose.yaml"
README = REPO / "README.md"
DEPLOYMENT = REPO / "docs" / "DEPLOYMENT.md"


def test_config_example_parses_and_loads(tmp_path: Path) -> None:
    """It is copied to config.toml verbatim; it has to actually work."""
    target = tmp_path / "config.toml"
    target.write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(target, env={})
    assert cfg.source.profile == "ftmo"
    assert cfg.serve.port == ServeConfig.port


def test_config_example_documents_every_current_event_type() -> None:
    """0.8 replaced four coarse types with seven; the example listed the old set."""
    data = tomllib.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    documented = set(data["events"]["summaries"])
    expected = set(DEFAULT_SUMMARIES)
    assert documented == expected, (
        "config.example.toml [events.summaries] drifted from DEFAULT_SUMMARIES; "
        f"missing {expected - documented}, extra {documented - expected}"
    )


def test_config_example_summaries_match_the_defaults() -> None:
    data = tomllib.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    assert data["events"]["summaries"] == DEFAULT_SUMMARIES


def test_compose_publishes_the_port_the_readme_tells_people_to_use() -> None:
    """The self-host blocker: compose bound 127.0.0.1:8133 while the README
    said to subscribe on :8080, so `docker compose up -d` produced a feed
    nobody outside the host could reach."""
    compose = COMPOSE.read_text(encoding="utf-8")
    assert f'"${{PORT:-{ServeConfig.port}}}:{ServeConfig.port}"' in compose
    assert "127.0.0.1:8133" not in compose
    assert f"your-vps:{ServeConfig.port}/feed.ics" in README.read_text(encoding="utf-8")


def test_the_public_instances_proxy_setup_lives_in_the_deployment_doc() -> None:
    """One maintainer's Caddy arrangement belongs in docs, not in everyone's
    shipped default."""
    deployment = DEPLOYMENT.read_text(encoding="utf-8")
    assert "127.0.0.1:8133" in deployment
    assert "8133" not in COMPOSE.read_text(encoding="utf-8")


def test_deployment_doc_proxies_the_port_it_tells_you_to_bind() -> None:
    """The stale version told readers to bind :8080 and proxy :8080 while the
    shipped compose used :8133 — a forker following it got a dead upstream."""
    deployment = DEPLOYMENT.read_text(encoding="utf-8")
    binding = 'ports:\n      - "127.0.0.1:8133:8080"'
    assert binding in deployment, "the loopback recipe must show the port it proxies"
    assert "reverse_proxy 127.0.0.1:8133" in deployment


def test_env_example_documents_every_secret_the_config_reads() -> None:
    env_example = (REPO / ".env.example").read_text(encoding="utf-8")
    for name in (
        "LLM_API_KEY",
        "DISCORD_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "WEBHOOK_URL",
    ):
        assert name in env_example, f"{name} is read by config.load_config but undocumented"


def test_dockerfile_installs_from_the_lock_file() -> None:
    """Unpinned installs reached the auto-deploying server directly."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile
    assert "-c requirements.lock" in dockerfile


def test_ci_builds_the_docker_image() -> None:
    """A broken Dockerfile used to reach production before it reached a human."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker build" in ci


def test_lint_tools_are_pinned_to_the_locked_versions() -> None:
    """An unpinned ruff turns a contributor's green build red on someone
    else's release schedule."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    locked = dict(
        line.split("==", 1)
        for line in (REPO / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if "==" in line
    )
    for tool in ("ruff", "mypy"):
        pin = next(d for d in dev if d.startswith(tool))
        assert pin == f"{tool}=={locked[tool].strip()}", (
            f"{tool} pin {pin!r} disagrees with requirements.lock"
        )


# -- shipped source profiles ----------------------------------------------


def _shipped_profiles() -> list[str]:
    from ftmo_calendar.sources.profile import available_profiles

    return [n for n in available_profiles() if n != "example-firm"]


def test_every_shipped_firm_is_listed_in_the_readme() -> None:
    """A firm nobody can discover is a firm nobody benefits from.

    The profiles directory is the source of truth; this fails when one is added
    without telling anyone, which is how the FTMO-only framing survived having
    a config-driven scraper in the first place.
    """
    readme = README.read_text(encoding="utf-8")
    for name in _shipped_profiles():
        assert f"`{name}`" in readme, f"profile {name!r} is shipped but not listed in README.md"


def test_every_shipped_firm_is_offered_in_the_config_example() -> None:
    example = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    for name in _shipped_profiles():
        assert name in example, f"profile {name!r} is shipped but absent from config.example.toml"


def test_every_shipped_firm_has_a_recorded_fixture() -> None:
    """CONTRIBUTING promises parse tests run against pages the site really served."""
    for name in _shipped_profiles():
        listing = REPO / "tests" / "fixtures" / name / "listing.html"
        assert listing.exists(), f"profile {name!r} ships without a recorded fixture"
        head = listing.read_text(encoding="utf-8")[:400]
        assert "Recorded from" in head, f"{listing} is not a recorded page"


def test_every_shipped_firm_declares_a_timezone_decision() -> None:
    """The project's core failure mode is a wrong hour; silence is not allowed.

    Either the profile names the zone announcements are read in, or it declares
    that no zone can be assumed and the announcement must state its own offset.
    """
    from zoneinfo import ZoneInfo

    from ftmo_calendar.sources.profile import load_profile

    for name in _shipped_profiles():
        profile = load_profile(name)
        assert profile.timezone, f"{name} states no timezone"
        ZoneInfo(profile.timezone)  # raises if it is not a real zone
        assert profile.post_key_prefix, f"{name} has no post_key_prefix"
        assert profile.prompt_hints.strip(), f"{name} ships no prompt hints"
