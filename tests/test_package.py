import subprocess
import sys
from importlib.metadata import entry_points

import pytest

from prop_firm_calendar import __version__


def test_version() -> None:
    assert __version__


# -- the pre-0.9 names keep working ----------------------------------------


def test_both_console_scripts_are_installed() -> None:
    """The Docker CMD, cron lines and systemd units in the wild say ftmo-calendar."""
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts["prop-firm-calendar"] == "prop_firm_calendar.cli:entry"
    assert scripts["ftmo-calendar"] == "prop_firm_calendar.cli:entry"


def test_the_old_import_name_resolves_to_the_same_modules() -> None:
    with pytest.warns(DeprecationWarning, match="prop_firm_calendar"):
        import ftmo_calendar  # noqa: F401
    import ftmo_calendar.cli
    from ftmo_calendar.sources.profile import load_profile

    import prop_firm_calendar.cli
    from prop_firm_calendar.sources import profile

    assert ftmo_calendar.__version__ == prop_firm_calendar.__version__
    # The same objects, not copies: patching through one name patches both.
    assert ftmo_calendar.cli is prop_firm_calendar.cli
    assert load_profile is profile.load_profile
    # And the real module keeps its real identity.
    assert prop_firm_calendar.cli.__spec__ is not None
    assert prop_firm_calendar.cli.__spec__.name == "prop_firm_calendar.cli"


def test_monkeypatching_through_the_old_name_reaches_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prop_firm_calendar.cli

    monkeypatch.setattr("ftmo_calendar.cli.EXIT_CONFIG", 99)
    assert prop_firm_calendar.cli.EXIT_CONFIG == 99


def test_the_old_name_warns_in_a_fresh_interpreter() -> None:
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import ftmo_calendar"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "renamed to 'prop_firm_calendar'" in result.stderr
    ok = subprocess.run(
        [
            sys.executable,
            "-W",
            "ignore::DeprecationWarning",
            "-c",
            "import ftmo_calendar.pipeline, prop_firm_calendar.pipeline as p; "
            "assert ftmo_calendar.pipeline is p",
        ],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr
