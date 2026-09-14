"""The one-line installer: config merging, backups, and dry runs.

`scripts/install_mcp.py` is deliberately importable and side-effect free, so
its logic can be tested without installing anything or touching a real host
config. The pip/self-test steps are skipped here (they are covered by the
selftest itself) — what matters is that the *JSON surgery* is safe.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install_mcp.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_mcp", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_mcp"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def installer():
    return load_installer()


# ─── entry construction ───────────────────────────────────────────────


def test_entry_uses_an_absolute_interpreter(installer):
    entry = installer.build_entry(
        "/opt/py/bin/python",
        robot="R1",
        ip="192.168.1.5",
        sn="",
        key="",
        region="auto",
        safe_mode=False,
        extra_env={},
    )
    assert entry["type"] == "stdio"
    assert entry["command"] == "/opt/py/bin/python"
    # `-m ... serve` rather than the console script: agent hosts do not inherit
    # the shell's PATH, so bin/ may not be on it.
    assert entry["args"] == ["-m", "unitree_robot_agent.cli", "serve"]
    assert entry["env"]["UNITREE_AGENT_ROBOT"] == "R1"
    assert entry["env"]["UNITREE_ROBOT_IP"] == "192.168.1.5"
    assert entry["description"]


def test_entry_carries_key_and_safe_mode_when_given(installer):
    entry = installer.build_entry(
        "/opt/py/bin/python",
        robot="Go2",
        ip="",
        sn="B42D2000XXXXXXXX",
        key="a" * 32,
        region="cn",
        safe_mode=True,
        extra_env={"UNITREE_AGENT_ALLOW_DANGEROUS": "false"},
    )
    assert entry["env"]["UNITREE_AES_128_KEY"] == "a" * 32
    assert entry["env"]["UNITREE_AGENT_REGION"] == "cn"
    assert entry["env"]["UNITREE_AGENT_SAFE_MODE"] == "true"
    assert entry["env"]["UNITREE_AGENT_ALLOW_DANGEROUS"] == "false"
    assert "UNITREE_ROBOT_IP" not in entry["env"], "no empty IP should be written"


# ─── host paths ───────────────────────────────────────────────────────


def test_host_paths_honour_the_config_dir_override(installer, tmp_path):
    paths = installer.host_paths(tmp_path)
    assert paths["codebuddy"] == tmp_path / "mcp.json"
    assert paths["workspace"].name == "mcp.json"
    assert paths["claude-desktop"].name == "claude_desktop_config.json"


def test_host_paths_default_to_the_users_home(installer):
    paths = installer.host_paths()
    assert paths["codebuddy"] == Path.home() / ".codebuddy" / "mcp.json"


# ─── registering ──────────────────────────────────────────────────────


def make_entry(installer, **overrides):
    kwargs = dict(
        robot="R1", ip="10.0.0.2", sn="", key="", region="auto", safe_mode=False, extra_env={}
    )
    kwargs.update(overrides)
    return installer.build_entry("/opt/py/bin/python", **kwargs)


def test_register_creates_the_file_and_directory(installer, tmp_path):
    target = tmp_path / "nested" / "mcp.json"
    assert installer.register("codebuddy", target, make_entry(installer), dry_run=False) is True

    payload = json.loads(target.read_text())
    assert list(payload["mcpServers"]) == ["unitree-robot"]
    assert payload["mcpServers"]["unitree-robot"]["command"] == "/opt/py/bin/python"


def test_register_preserves_other_servers(installer, tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"})
    )
    installer.register("codebuddy", target, make_entry(installer), dry_run=False)

    payload = json.loads(target.read_text())
    assert set(payload["mcpServers"]) == {"other", "unitree-robot"}
    assert payload["theme"] == "dark", "unrelated keys must survive"


def test_register_is_idempotent_and_leaves_a_backup(installer, tmp_path):
    target = tmp_path / "mcp.json"
    installer.register("codebuddy", target, make_entry(installer, ip="10.0.0.2"), dry_run=False)
    installer.register("codebuddy", target, make_entry(installer, ip="10.0.0.9"), dry_run=False)

    payload = json.loads(target.read_text())
    assert payload["mcpServers"]["unitree-robot"]["env"]["UNITREE_ROBOT_IP"] == "10.0.0.9"
    assert list(payload["mcpServers"]) == ["unitree-robot"], "no duplicate entry"

    backups = list(tmp_path.glob("mcp.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["mcpServers"]["unitree-robot"]["env"][
        "UNITREE_ROBOT_IP"
    ] == "10.0.0.2"


def test_register_refuses_to_clobber_invalid_json(installer, tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text("{ this is not json")
    assert installer.register("codebuddy", target, make_entry(installer), dry_run=False) is False
    assert target.read_text() == "{ this is not json", "the broken file must be untouched"


def test_register_refuses_a_non_object_root(installer, tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text("[1, 2, 3]")
    assert installer.register("codebuddy", target, make_entry(installer), dry_run=False) is False
    assert target.read_text() == "[1, 2, 3]"


def test_register_refuses_a_non_object_mcpservers(installer, tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": ["nope"]}))
    assert installer.register("codebuddy", target, make_entry(installer), dry_run=False) is False


def test_dry_run_changes_nothing(installer, tmp_path, capsys):
    target = tmp_path / "mcp.json"
    assert installer.register("codebuddy", target, make_entry(installer), dry_run=True) is True
    assert not target.exists()
    assert "would write" in capsys.readouterr().out


# ─── main() ───────────────────────────────────────────────────────────


def test_main_dry_run_writes_nothing(installer, tmp_path, capsys):
    target = tmp_path / "mcp.json"
    code = installer.main(
        [
            "--robot", "R1",
            "--ip", "10.0.0.2",
            "--host", "codebuddy",
            "--config-dir", str(tmp_path),
            "--python", sys.executable,
            "--dry-run",
        ]
    )
    assert code == 0
    assert not target.exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "mcpServers" in out


def test_main_skips_pip_and_verify_when_asked(installer, tmp_path):
    target = tmp_path / "mcp.json"
    code = installer.main(
        [
            "--robot", "Go2",
            "--ip", "10.0.0.3",
            "--host", "codebuddy",
            "--config-dir", str(tmp_path),
            "--python", sys.executable,
            "--no-install",
            "--no-verify",
        ]
    )
    assert code == 0
    payload = json.loads(target.read_text())
    entry = payload["mcpServers"]["unitree-robot"]
    assert entry["env"]["UNITREE_AGENT_ROBOT"] == "Go2"
    assert entry["command"] == sys.executable


def test_main_host_none_only_prints_the_snippet(installer, tmp_path, capsys):
    code = installer.main(
        [
            "--host", "none",
            "--config-dir", str(tmp_path),
            "--no-install",
            "--no-verify",
        ]
    )
    assert code == 0
    assert not (tmp_path / "mcp.json").exists()
    out = capsys.readouterr().out  # read once — the buffer is drained on read
    assert "mcpServers" in out
    assert "unitree-robot" in out


def test_main_custom_server_name(installer, tmp_path):
    installer.main(
        [
            "--name", "my-robot",
            "--host", "codebuddy",
            "--config-dir", str(tmp_path),
            "--no-install",
            "--no-verify",
        ]
    )
    payload = json.loads((tmp_path / "mcp.json").read_text())
    assert list(payload["mcpServers"]) == ["my-robot"]


# ─── regressions: relative paths, extra, exit code, permissions ───────


def test_venv_records_an_absolute_interpreter(installer, tmp_path, monkeypatch, capsys):
    """`--venv .venv` used to be recorded verbatim, so the host was told to
    run ".venv/bin/python" relative to a directory it never cd's into."""
    monkeypatch.chdir(tmp_path)
    code = installer.main(
        ["--venv", ".venv", "--host", "none", "--no-install", "--no-verify", "--dry-run"]
    )
    assert code == 0
    out = capsys.readouterr().out
    expected = str(tmp_path / ".venv" / "bin" / "python")
    assert expected in out
    assert '"command": ".venv' not in out
    # The symlink must survive: resolving bin/python lands on the base
    # interpreter, which has none of this package's dependencies.
    assert '"command": ' + json.dumps(expected) in out


def test_install_package_keeps_the_agent_extra_and_honours_no_editable(installer, capsys):
    assert installer.install_package(sys.executable, editable=False, dry_run=True) is True
    plain = capsys.readouterr().out
    assert "[agent]" in plain, "a non-editable install still needs the extra"
    assert " -e " not in plain

    assert installer.install_package(sys.executable, editable=True, dry_run=True) is True
    editable = capsys.readouterr().out
    assert "[agent]" in editable and " -e " in editable


def test_main_returns_non_zero_when_every_host_fails(installer, tmp_path, capsys):
    (tmp_path / "mcp.json").write_text("{ not json")
    code = installer.main(
        ["--host", "cursor", "--config-dir", str(tmp_path), "--no-install", "--no-verify"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "Nothing was registered" in out
    assert "mcpServers" in out, "the manual snippet is the fallback"


def test_register_preserves_the_file_mode(installer, tmp_path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {}}))
    target.chmod(0o600)

    assert installer.register("cursor", target, make_entry(installer), dry_run=False) is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_register_writes_through_a_symlinked_config(installer, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"mcpServers": {}}))
    link = tmp_path / "mcp.json"
    link.symlink_to(real)

    assert installer.register("cursor", link, make_entry(installer), dry_run=False) is True
    assert link.is_symlink() is True, "replacing the link would orphan the real file"
    assert "unitree-robot" in json.loads(real.read_text())["mcpServers"]
