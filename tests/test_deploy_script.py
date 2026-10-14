"""Run deployment checks with stubbed system commands and temporary files."""

from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]
Sandbox = runpy.run_path(str(ROOT / "tools/gate_deploy_script_unit.py"))["Sandbox"]


@pytest.mark.parametrize("mode", ["--update-only", "--reconfigure"])
def test_env_values_are_read_without_executing_shell_commands(tmp_path, mode):
    marker = tmp_path / "executed"
    token = f'token-$(touch {marker})-`touch {marker}`'
    box = Sandbox(tmp_path)
    box.setup(env_text=f'PORT=8000\nMCP_AUTH_TOKEN="{token}"\n', unit_exists=True)

    proc, records = box.run([mode], stdin="\n" * 5)

    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), ".env must be read as data"
    assert f"Bearer {token}" in records


def test_interactive_credentials_round_trip_as_literal_values(tmp_path):
    token = 'token-$literal-`literal`-"quoted"-\\path'
    box = Sandbox(tmp_path)
    box.setup(env_text="PORT=8000\n", unit_exists=True)

    proc, _ = box.run(["--reconfigure"], stdin=f"\n{token}\n\n\n\n")
    assert proc.returncode == 0, proc.stderr

    proc, records = box.run(["--update-only"])
    assert proc.returncode == 0, proc.stderr
    assert f"Bearer {token}" in records.splitlines()[-1]
    assert token not in proc.stdout + proc.stderr


@pytest.mark.parametrize("status", ["403", "404", "405", "500"])
def test_failed_authentication_probe_fails_deployment(tmp_path, status):
    box = Sandbox(tmp_path)
    box.setup(
        env_text="PORT=8000\nMCP_AUTH_TOKEN=example-token-123456\n",
        unit_exists=True,
        messages=status,
    )

    proc, _ = box.run(["--update-only"])

    assert proc.returncode != 0
    assert "更新完成" not in proc.stdout


def test_unit_creation_treats_directory_names_as_data(tmp_path):
    box = Sandbox(tmp_path / "$(touch executed) space")
    box.setup(
        env_text="PORT=8000\nMCP_AUTH_TOKEN=example-token-123456\n",
        unit_exists=False,
    )

    proc, records = box.run(["--replace-unit"], stdin="\n" * 5)

    assert proc.returncode == 0, proc.stderr
    assert not (box.work / "executed").exists()
    unit = (box.systemd / "image-search-mcp.service").read_text()
    assert f'EnvironmentFile="{box.work}/.env"' in unit
    assert "sudo\tbash" not in records


@pytest.mark.parametrize("configured,entered,expected", [("TraceMoe", "", "TraceMoe"), ("Yandex", "all", "All")])
def test_reconfiguration_preserves_or_changes_default_engine(tmp_path, configured, entered, expected):
    box = Sandbox(tmp_path)
    box.setup(
        env_text=f"PORT=8000\nMCP_AUTH_TOKEN=example-token-123456\nIMAGE_SEARCH_DEFAULT_ENGINE={configured}\n",
        unit_exists=True,
    )
    proc, _ = box.run(["--reconfigure"], stdin="\n" * 5 + entered + "\n")
    assert proc.returncode == 0, proc.stderr
    contents = (box.work / ".env").read_text()
    assert f"IMAGE_SEARCH_DEFAULT_ENGINE={expected}\n" in contents
    assert contents.count("IMAGE_SEARCH_DEFAULT_ENGINE=") == 1
