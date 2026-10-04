"""Tests for cron/daily-sync.sh, the scheduled sync run by the cron container."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from expense_tracker import cli

SCRIPT = Path(__file__).resolve().parent.parent / "cron" / "daily-sync.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run_one_sync(tmp_path: Path) -> list[str]:
    """The `python` calls of one sync in which every step succeeds."""
    calls, _ = _run_sync(tmp_path)
    return calls


def _run_sync(tmp_path: Path, failing: str = "") -> tuple[list[str], str]:
    """Run the script once in a fake /app laid out like the Docker image.

    The image has the package and cron/ but no scripts/ and no .venv: its
    dependencies are installed into the system Python. `python` is stubbed
    to log its arguments, and the first `sleep` ends the script: reaching
    it means the daily loop is still alive. The stub exits 1 for the
    sub-commands named in `failing`.

    Returns the logged calls and the script's output.
    """
    app = tmp_path / "app"
    (app / "cron").mkdir(parents=True)
    (app / "data").mkdir()
    (app / "data" / "email-config.json").write_text("{}", encoding="utf-8")
    shutil.copy(SCRIPT, app / "cron" / "daily-sync.sh")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    stub = bin_dir / "python"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {shlex.quote(str(log))}\n'
        'for name in $FAILING; do case " $* " in *" $name "*) exit 1;; esac; done\n'
    )
    stub.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "APP": str(app),
        "HOME": str(tmp_path),
        "FAILING": failing,
    }
    driver = (
        'cd() { builtin cd "$APP"; }; sleep() { exit 0; }; '
        "export -f cd sleep; "
        'exec bash "$APP/cron/daily-sync.sh"'
    )
    result = subprocess.run(
        ["bash", "-c", driver], env=env, capture_output=True, text=True, timeout=30
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return calls, output


def test_sync_runs_every_step_without_a_venv(tmp_path):
    calls = _run_one_sync(tmp_path)

    assert [c.split()[0] for c in calls] == ["bank_ingest.py"] * 3


def test_sync_commands_are_accepted_by_the_cli(tmp_path, monkeypatch):
    calls = _run_one_sync(tmp_path)
    assert calls

    for call in calls:
        argv = shlex.split(call)
        seen = []
        for name in ("cmd_fetch", "cmd_auto", "cmd_pdf"):
            monkeypatch.setattr(cli, name, lambda args: seen.append(args))
        monkeypatch.setattr(sys, "argv", argv)

        cli.main()  # argparse exits with status 2 on an unknown option

        assert len(seen) == 1, call
        assert seen[0].quiet, call


def _steps(calls: list[str]) -> list[str]:
    return [shlex.split(c)[-1] for c in calls]


def test_statement_that_cannot_be_imported_does_not_end_the_sync(tmp_path):
    """`auto` exits 1 when a file fails or is skipped. Under `set -e` that
    ended the script: no PDF, no copy, and a container restarting into the
    same failure instead of waiting for the next day."""
    calls, _ = _run_sync(tmp_path, failing="auto")

    assert _steps(calls) == ["fetch", "auto", "pdf"]


@pytest.mark.parametrize("step", ["fetch", "auto", "pdf"])
def test_failed_step_is_reported_as_an_error(tmp_path, step):
    calls, output = _run_sync(tmp_path, failing=step)

    assert _steps(calls) == ["fetch", "auto", "pdf"]
    errors = [line for line in output.splitlines() if "ERROR" in line]
    assert len(errors) == 2, output
    assert step in errors[0]
    assert errors[1].startswith("=== Sync finished WITH ERRORS at ")
    assert "=== Sync complete" not in output


def test_successful_sync_reports_no_error(tmp_path):
    _, output = _run_sync(tmp_path)

    assert "ERROR" not in output
    assert "=== Sync complete at " in output


def test_deployment_guide_lists_the_script_as_shipped():
    """DEPLOYMENT.md tells the reader to create cron/daily-sync.sh from its
    listing: a stale copy there deploys the bugs fixed here."""
    guide = (SCRIPT.parent.parent / "DEPLOYMENT.md").read_text(encoding="utf-8")
    section = guide.split("## 4. Daily Sync Script", 1)[1]
    listing = section.split("```bash\n", 1)[1].split("```", 1)[0]

    assert listing == SCRIPT.read_text(encoding="utf-8")
