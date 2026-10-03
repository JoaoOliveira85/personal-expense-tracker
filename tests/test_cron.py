"""Tests for cron/daily-sync.sh, the scheduled sync run by the cron container."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "cron" / "daily-sync.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run_one_sync(tmp_path: Path) -> list[str]:
    """Run the script once in a fake /app laid out like the Docker image.

    The image has the package and cron/ but no scripts/ and no .venv: its
    dependencies are installed into the system Python. `python` is stubbed
    to log its arguments, and the first `sleep` ends the script.
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
    stub.write_text(f'#!/bin/sh\necho "$*" >> {shlex.quote(str(log))}\n')
    stub.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "APP": str(app),
        "HOME": str(tmp_path),
    }
    driver = (
        'cd() { builtin cd "$APP"; }; sleep() { exit 0; }; '
        "export -f cd sleep; "
        'exec bash "$APP/cron/daily-sync.sh"'
    )
    result = subprocess.run(
        ["bash", "-c", driver], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_sync_runs_every_step_without_a_venv(tmp_path):
    calls = _run_one_sync(tmp_path)

    assert [c.split()[0] for c in calls] == ["bank_ingest.py"] * 3

