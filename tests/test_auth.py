"""Cloud auth: region detection, session caching, refresh, key lookup.

The network is faked at the ``unitree_webrtc_connect.UnitreeCloud`` boundary,
so these tests exercise the real control flow — including the China-vs-global
trap that cost a round trip on the first live session — without touching the
internet.
"""

from __future__ import annotations

import json

import pytest
import unitree_webrtc_connect
from unitree_webrtc_connect import RobotDevice, UnitreeCloudError

from unitree_robot_agent.config import config_from_env
from unitree_robot_agent.core.audit import AuditLog
from unitree_robot_agent.core.auth import CloudAuth, CloudSession

from .conftest import make_config

EMAIL = "user@example.com"
PASSWORD = "hunter2"
SN = "E39N1000Q6G8448E"
KEY = "0123456789abcdef0123456789abcdef"


class FakeCloud:
    """Stands in for UnitreeCloud. Records what happened to it."""

    def __init__(self, *, region, device_type="R1", access_token="", refresh_token=""):
        self.region = region
        self.device_type = device_type
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.login_calls = 0
        self.refresh_calls = 0
        FakeCloud.instances.append(self)

    # every instance, so tests can assert across constructions
    instances: list = []

    @classmethod
    def reset(cls):
        cls.instances = []

    # ── behaviour ────────────────────────────────────────────────────
    def login_email(self, email, password):
        self.login_calls += 1
        if email != EMAIL or password != PASSWORD:
            raise UnitreeCloudError("login/email", 1000, "email not register")
        if self.region != "cn":
            # Exactly what the real global endpoint said for a CN account.
            raise UnitreeCloudError("login/email", 1000, "email not register")
        self.access_token = "access-from-login"
        self.refresh_token = "refresh-from-login"
        return self.access_token

    def list_devices(self):
        if self.access_token in ("", "expired.invalid.token"):
            raise UnitreeCloudError("device/bind/list", 1001, "token expired")
        return [
            RobotDevice(sn=SN, alias="R1_04846", series="R1", model="Air", online=True, key=KEY)
        ]

    def _refresh(self):
        self.refresh_calls += 1
        if not self.refresh_token or self.refresh_token in ("y", "dead"):
            return False
        self.access_token = "access-from-refresh"
        return True


@pytest.fixture(autouse=True)
def fake_cloud(monkeypatch, tmp_path):
    FakeCloud.reset()
    monkeypatch.setattr(unitree_webrtc_connect, "UnitreeCloud", FakeCloud)
    monkeypatch.setenv("UNITREE_AGENT_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "UNITREE_AGENT_EMAIL",
        "UNITREE_AGENT_PASSWORD",
        "UNITREE_AES_128_KEY",
        "UNITREE_AGENT_ACCESS_TOKEN",
        "UNITREE_AGENT_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    return FakeCloud


def make_auth(**overrides) -> tuple:
    """Config + CloudAuth. `config_from_env` drops None overrides and picks up
    the autouse fixture's temp state dir, so callers only pass what they mean."""
    cfg = config_from_env(robot="R1", **overrides)
    return cfg, CloudAuth(cfg, AuditLog(enabled=False))


# ─── session cache ────────────────────────────────────────────────────


async def test_region_is_auto_detected(tmp_path):
    """A CN account answers 'email not register' on global; we must retry cn."""
    cfg, auth = make_auth(username=EMAIL, password=PASSWORD, region="auto")
    lookup = await auth.resolve_aes_key(SN)

    assert lookup.ok is True
    assert lookup.source == "cloud"
    assert lookup.region == "cn"
    assert cfg.resolved_region == "cn"
    assert cfg.connection_region == "cn"

    regions_tried = [c.region for c in FakeCloud.instances]
    assert regions_tried == ["global", "cn"]


async def test_session_is_cached_and_reused_without_a_password(tmp_path):
    cfg, auth = make_auth(username=EMAIL, password=PASSWORD, region="auto")
    await auth.resolve_aes_key(SN)
    assert (cfg.state_dir / "session.json").exists()

    # Always-on key cache would short-circuit; drop it to exercise the session.
    (cfg.state_dir / "keys.json").unlink()
    FakeCloud.reset()  # only look at what the second run does

    cfg2, auth2 = make_auth(region="auto")          # no email, no password
    lookup = await auth2.resolve_aes_key(SN)
    assert lookup.ok is True
    assert auth2.session is not None and auth2.session.source == "cache"
    assert cfg2.resolved_region == "cn", "the working region must be restored too"
    assert all(c.login_calls == 0 for c in FakeCloud.instances)


async def test_expired_access_token_is_refreshed_and_persisted(tmp_path):
    cfg, auth = make_auth(username=EMAIL, password=PASSWORD)
    await auth.resolve_aes_key(SN)
    session_path = cfg.state_dir / "session.json"
    (cfg.state_dir / "keys.json").unlink()  # else the key cache answers first
    payload = json.loads(session_path.read_text())
    account = next(iter(payload))
    payload[account]["access_token"] = "expired.invalid.token"
    session_path.write_text(json.dumps(payload))

    cfg2, auth2 = make_auth()
    lookup = await auth2.resolve_aes_key(SN)

    assert lookup.ok is True
    assert auth2.session is not None and auth2.session.source == "refresh"
    stored = json.loads(session_path.read_text())
    assert list(stored) == [account], "the cache key must not drift between runs"
    assert stored[account]["access_token"] == "access-from-refresh"


async def test_dead_session_without_password_explains_itself():
    cfg, auth = make_auth(username=EMAIL, password=PASSWORD)
    await auth.resolve_aes_key(SN)
    session_path = cfg.state_dir / "session.json"
    (cfg.state_dir / "keys.json").unlink()
    payload = json.loads(session_path.read_text())
    account = next(iter(payload))
    payload[account]["access_token"] = "expired.invalid.token"
    payload[account]["refresh_token"] = "dead"
    session_path.write_text(json.dumps(payload))

    cfg2, auth2 = make_auth()
    lookup = await auth2.resolve_aes_key(SN)

    assert lookup.ok is False
    assert "no longer valid" in lookup.note.lower()


async def test_access_token_env_skips_login(monkeypatch):
    monkeypatch.setenv("UNITREE_AGENT_ACCESS_TOKEN", "manual-token")
    cfg = config_from_env(robot="R1")
    auth = CloudAuth(cfg, AuditLog(enabled=False))
    FakeCloud.reset()

    lookup = await auth.resolve_aes_key(SN)
    assert lookup.ok is True
    assert auth.session is not None and auth.session.source == "env"
    assert all(c.login_calls == 0 for c in FakeCloud.instances)


# ─── key lookup ───────────────────────────────────────────────────────


async def test_explicit_key_never_touches_the_cloud():
    cfg, auth = make_auth(aes_128_key=KEY)
    FakeCloud.reset()
    lookup = await auth.resolve_aes_key()
    assert (lookup.ok, lookup.source) == (True, "config")
    assert FakeCloud.instances == []


async def test_no_credentials_returns_a_note_instead_of_hanging():
    cfg, auth = make_auth()
    FakeCloud.reset()
    lookup = await auth.resolve_aes_key(SN)
    assert lookup.ok is False
    assert "skipped" in lookup.note
    assert FakeCloud.instances == []


async def test_unknown_serial_lists_what_is_bound():
    cfg, auth = make_auth(username=EMAIL, password=PASSWORD)
    lookup = await auth.resolve_aes_key("WRONG-SN")
    assert lookup.ok is False
    assert "WRONG-SN" in lookup.note
    assert SN in lookup.note
    # keys must be masked in anything reported back
    assert all("key" not in d or d["key"].endswith("…") or "****" in d.get("key", "")
               for d in lookup.devices)


async def test_only_bound_device_is_picked_when_no_sn_configured():
    cfg, auth = make_auth(username=EMAIL, password=PASSWORD)
    lookup = await auth.resolve_aes_key(None)
    assert lookup.ok is True
    assert lookup.sn == SN
    assert cfg.serial_number == SN
    assert cfg.load_cached_aes_key() == KEY


# ─── helpers / persistence ────────────────────────────────────────────


def test_region_candidates_respects_the_fallback_switch():
    cfg = make_config(region="cn", region_auto_fallback=True)
    assert CloudAuth(cfg)._region_candidates() == ["cn", "global"]
    cfg_no = make_config(region="cn", region_auto_fallback=False)
    assert CloudAuth(cfg_no)._region_candidates() == ["cn"]
    cfg_auto = make_config(region="auto")
    assert CloudAuth(cfg_auto)._region_candidates() == ["global", "cn"]


def test_config_region_is_normalised():
    assert make_config(region="CN").region == "cn"
    assert make_config(region="nonsense").region == "auto"
    assert make_config(region="").region == "auto"


def test_wrong_region_detection_matches_the_real_message():
    assert CloudAuth._looks_like_wrong_region(
        "login/email failed: code=1000 msg='email not register'"
    )
    assert not CloudAuth._looks_like_wrong_region("login/email failed: code=1002 bad password")


def test_session_round_trip_and_stable_key(tmp_path):
    cfg = make_config(state_dir=tmp_path)
    auth = CloudAuth(cfg, AuditLog(enabled=False))
    session = CloudSession(
        region="cn", email=EMAIL, access_token="a", refresh_token="r", source="login"
    )
    auth.store_session(session)

    payload = json.loads((tmp_path / "session.json").read_text())
    assert list(payload) == [EMAIL]

    # A run without any configured username still finds it.
    cfg2 = make_config(state_dir=tmp_path)
    auth2 = CloudAuth(cfg2, AuditLog(enabled=False))
    loaded = auth2.load_session()
    assert loaded is not None and loaded.access_token == "a" and loaded.region == "cn"

    auth2.clear_session()
    assert auth2.load_session() is None


def test_session_file_is_not_world_readable(tmp_path):
    cfg = make_config(state_dir=tmp_path)
    auth = CloudAuth(cfg, AuditLog(enabled=False))
    auth.store_session(CloudSession(region="cn", email=EMAIL, access_token="a"))
    mode = oct((tmp_path / "session.json").stat().st_mode)[-3:]
    assert mode == "600", mode
