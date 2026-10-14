"""Validate the check script's command-line modes."""

from pathlib import Path
import subprocess


def test_conflicting_modes_cannot_report_success_without_running_checks():
    script = Path(__file__).resolve().parents[1] / "check.sh"
    result = subprocess.run(
        ["bash", str(script), "--fast", "--reverse-only"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "不能同时使用" in result.stderr
    assert "全部通过" not in result.stdout
