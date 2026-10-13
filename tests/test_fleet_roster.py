"""Fleet identity: the roster, the specs, and the per-device key store.

The tests that matter most here are the ones that pin down *identity* — that
two robots of the same model stay distinguishable everywhere, and that one
robot's AES key can never be handed to another.
"""

from __future__ import annotations

import json

import pytest

from unitree_robot_agent.errors import (
    FleetConfigError,
    UnknownRobot,
)
from unitree_robot_agent.fleet import KeyStore, RobotSpec, Roster, load_roster
from unitree_robot_agent.fleet.spec import normalise_key

KEY1 = "0123456789abcdef0123456789abcdef"
KEY2 = "fedcba9876543210fedcba9876543210"
DOG1_SN = "B42D2000AAAA"
DOG2_SN = "B42D2000BBBB"


def dog1(**overrides) -> RobotSpec:
    base = dict(
        robot_id="dog1",
        model="Go2",
        connection="sta-l",
        ip="192.168.31.245",
        serial_number=DOG1_SN,
        aes_128_key=KEY1,
        role="lead",
    )
    base.update(overrides)
    return RobotSpec(**base)


def dog2(**overrides) -> RobotSpec:
    base = dict(
        robot_id="dog2",
        model="Go2",
        connection="sta-l",
        ip="192.168.31.246",
        serial_number=DOG2_SN,
        aes_128_key=KEY2,
        role="wing",
    )
    base.update(overrides)
    return RobotSpec(**base)


def roster_mapping(**overrides) -> dict:
    base = {
        "state_dir": "~/.unitree-agent",
        "default": "dog1",
        "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": DOG1_SN},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": DOG2_SN},
        ],
    }
    base.update(overrides)
    return base


# ─── spec ─────────────────────────────────────────────────────────────


def test_spec_accepts_the_field_names_an_operator_would_type():
    spec = RobotSpec.from_mapping(
        {"id": "scout", "robot": "go2", "connection": "LocalSTA", "sn": DOG1_SN}
    )
    assert spec.robot_id == "scout"
    assert spec.model == "Go2"          # normalised to the driver's casing
    assert spec.connection == "sta-l"   # alias, not a literal
    assert spec.target == DOG1_SN       # SN beats IP as the log-safe target


def test_spec_requires_an_id():
    with pytest.raises(FleetConfigError, match="no `id`"):
        RobotSpec.from_mapping({"model": "Go2", "ip": "192.168.31.245"})


def test_spec_rejects_an_unknown_model():
    with pytest.raises(FleetConfigError, match="expected one of Go2, G1, R1"):
        RobotSpec.from_mapping({"id": "x", "model": "B2", "ip": "1.2.3.4"})


def test_sta_l_needs_an_address():
    with pytest.raises(FleetConfigError, match="neither `ip` nor `sn`"):
        RobotSpec.from_mapping({"id": "dog1", "model": "Go2", "connection": "sta-l"})


def test_remote_needs_a_serial_number():
    with pytest.raises(FleetConfigError, match="no `sn`"):
        RobotSpec.from_mapping(
            {"id": "dog1", "model": "Go2", "connection": "sta-t", "ip": "1.2.3.4"}
        )


def test_ap_mode_needs_no_address_because_the_robot_is_the_ap():
    spec = RobotSpec.from_mapping({"id": "dog1", "model": "Go2", "connection": "ap"})
    assert spec.connection == "ap"


def test_spec_redacted_never_leaks_the_key():
    payload = json.dumps(dog1().redacted())
    assert KEY1 not in payload
    assert '"has_key": true' in payload


@pytest.mark.parametrize(
    "written",
    ["0123456789ABCDEF0123456789ABCDEF", "0123-4567-89ab-cdef-0123-4567-89ab-cdef"],
)
def test_keys_are_canonicalised_regardless_of_how_they_are_written(written):
    assert normalise_key(written) == KEY1


def test_a_malformed_key_is_rejected_at_load_time_not_at_handshake_time():
    # Failing in the roster is the whole point: at handshake time this surfaces
    # as a GCM tag error that reads like a hardware fault.
    with pytest.raises(FleetConfigError, match="32 hex characters"):
        normalise_key("nope")


# ─── roster ───────────────────────────────────────────────────────────


def test_roster_loads_and_resolves_by_id():
    roster = load_roster(roster_mapping())
    assert roster.ids == ("dog1", "dog2")
    assert roster.resolve("dog2").ip == "192.168.31.246"
    assert roster.resolve().robot_id == "dog1"  # the declared default


def test_omitted_id_is_refused_when_several_robots_are_configured():
    """Guessing here means moving the wrong dog."""
    roster = load_roster(roster_mapping(default=""))
    with pytest.raises(UnknownRobot, match="robot_id` is required"):
        roster.resolve()


def test_omitted_id_is_fine_with_exactly_one_robot():
    assert load_roster({"robots": [{"id": "only", "ip": "1.2.3.4"}]}).resolve().robot_id == "only"


def test_unknown_robot_lists_what_is_known():
    with pytest.raises(UnknownRobot, match="dog1, dog2"):
        load_roster(roster_mapping()).get("dog3")


def test_specs_get_their_own_state_directory():
    """One line that fixes key cache, session cache, snapshots and audit."""
    roster = load_roster(roster_mapping())
    assert roster.state_dir_for("dog1") != roster.state_dir_for("dog2")
    assert roster.state_dir_for("dog1").name == "dog1"


def test_duplicate_ids_are_rejected():
    with pytest.raises(FleetConfigError, match="Duplicate robot id"):
        load_roster(
            {
                "robots": [
                    {"id": "dog", "ip": "192.168.31.245"},
                    {"id": "dog", "ip": "192.168.31.246"},
                ]
            }
        )


def test_two_entries_pointing_at_one_serial_number_are_rejected():
    with pytest.raises(FleetConfigError, match="same robot"):
        load_roster(
            {
                "robots": [
                    {"id": "a", "sn": DOG1_SN},
                    {"id": "b", "sn": DOG1_SN, "ip": "192.168.31.246"},
                ]
            }
        )


def test_two_entries_pointing_at_one_ip_are_rejected():
    with pytest.raises(FleetConfigError, match="same robot"):
        load_roster(
            {
                "robots": [
                    {"id": "a", "ip": "192.168.31.245"},
                    {"id": "b", "ip": "192.168.31.245", "sn": DOG2_SN},
                ]
            }
        )


def test_a_default_naming_a_missing_robot_is_rejected():
    with pytest.raises(FleetConfigError, match="not on the roster"):
        load_roster(roster_mapping(default="dog9"))


def test_empty_roster_is_rejected_with_a_usable_message():
    with pytest.raises(FleetConfigError, match="empty `robots`"):
        load_roster({"robots": []})


def test_per_robot_options_override_fleet_defaults():
    roster = load_roster(
        {
            "defaults": {"idle_disconnect_s": 45, "safe_mode": True},
            "robots": [
                {"id": "dog1", "ip": "192.168.31.245", "options": {"idle_disconnect_s": 0}},
            ],
        }
    )
    merged = roster.options_for(roster.resolve("dog1"))
    assert merged["idle_disconnect_s"] == 0
    assert merged["safe_mode"] is True


def test_inline_json_and_a_file_are_both_accepted(tmp_path):
    inline = load_roster(json.dumps(roster_mapping()))
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(roster_mapping()), encoding="utf-8")
    assert inline.ids == load_roster(path).ids


def test_env_var_points_at_the_roster(tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(roster_mapping()), encoding="utf-8")
    monkeypatch.setenv("UNITREE_AGENT_ROSTER", str(path))
    assert load_roster().ids == ("dog1", "dog2")


def test_missing_roster_says_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("UNITREE_AGENT_ROSTER", raising=False)
    with pytest.raises(FleetConfigError, match="UNITREE_AGENT_ROSTER"):
        load_roster()


def test_roster_redacted_hides_keys_but_reports_their_presence():
    roster = Roster.from_mapping(
        {
            "robots": [
                {"id": "dog1", "ip": "192.168.31.245", "aes_128_key": KEY1},
                {"id": "dog2", "ip": "192.168.31.246"},
            ]
        }
    )
    payload = json.dumps(roster.redacted())
    assert KEY1 not in payload
    assert '"has_key": true' in payload
    assert '"has_key": false' in payload


# ─── key store: the bug this whole module exists to prevent ───────────


def test_second_same_model_robot_does_not_inherit_the_first_robots_key(tmp_path):
    """Regression: the family fallback in the old cache did exactly this.

    dog1 has a cached key. dog2 is configured by IP only and has none. The
    old lookup saw one entry for the "Go2" family and returned it — so dog2
    was handed dog1's key and failed the GCM tag check.
    """
    store = KeyStore(tmp_path / "keys.json")
    store.put(dog1(), KEY1)

    assert store.get(dog1()) == KEY1
    assert store.get(dog2()) is None


def test_a_legacy_family_cache_is_migrated_but_never_cross_assigned(tmp_path):
    """Old on-disk shape: {"Go2:<sn-or-ip>": "<key>"}."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"Go2:192.168.31.245": KEY1}), encoding="utf-8")
    store = KeyStore(path)

    # The robot that actually owns the entry still finds it...
    assert store.get(RobotSpec(robot_id="dog1", ip="192.168.31.245")) == KEY1
    # ...and the other Go2 does not, which is the whole point.
    assert store.get(RobotSpec(robot_id="dog2", ip="192.168.31.246")) is None


def test_legacy_serial_number_entries_are_recognised_as_serials(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"Go2:" + DOG1_SN: KEY1}), encoding="utf-8")
    store = KeyStore(path)
    assert store.get(RobotSpec(robot_id="dog1", serial_number=DOG1_SN)) == KEY1


def test_an_ip_entry_is_refused_for_a_different_robot_that_now_holds_that_ip(tmp_path):
    """DHCP recycling must not leak a key from the previous occupant."""
    store = KeyStore(tmp_path / "keys.json")
    store.put(
        RobotSpec(robot_id="old", ip="192.168.31.245", serial_number="SN-OLD"), KEY1
    )

    recycled = RobotSpec(robot_id="new", ip="192.168.31.245", serial_number="SN-NEW")
    assert store.get(recycled) is None


def test_a_key_fetched_by_serial_number_is_found_later_by_ip_alone(tmp_path):
    """Preserve the convenience the family fallback was reaching for."""
    store = KeyStore(tmp_path / "keys.json")
    store.put(dog1(), KEY1)

    by_ip_only = RobotSpec(robot_id="dog1", ip="192.168.31.245")
    assert store.get(by_ip_only) == KEY1


def test_a_key_fetched_by_ip_is_found_later_by_serial_number(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.put(RobotSpec(robot_id="dog1", ip="192.168.31.245"), KEY1)

    with_sn = RobotSpec(robot_id="dog1", ip="192.168.31.245", serial_number=DOG1_SN)
    assert store.get(with_sn) == KEY1


def test_each_robot_gets_its_own_key_back(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.put(dog1(), KEY1)
    store.put(dog2(), KEY2)
    assert store.get(dog1()) == KEY1
    assert store.get(dog2()) == KEY2


def test_forget_removes_only_that_robots_key(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.put(dog1(), KEY1)
    store.put(dog2(), KEY2)

    store.forget(dog1())

    assert store.get(dog1()) is None
    assert store.get(dog2()) == KEY2


def test_store_file_is_owner_only_and_survives_reload(tmp_path):
    import os
    import stat

    path = tmp_path / "keys.json"
    KeyStore(path).put(dog1(), KEY1)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"credential cache is {oct(mode)}, expected 0o600"
    assert KeyStore(path).get(dog1()) == KEY1


def test_a_corrupt_cache_is_a_miss_not_a_crash(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text("{not json", encoding="utf-8")
    assert KeyStore(path).get(dog1()) is None


def test_store_does_not_write_a_key_it_cannot_canonicalise(tmp_path):
    path = tmp_path / "keys.json"
    with pytest.raises(FleetConfigError):
        KeyStore(path).put(dog1(), "garbage")
    assert not path.exists()


def test_known_ids_are_identities_never_key_material(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.put(dog1(), KEY1)
    ids = store.known_ids()
    assert f"sn:{DOG1_SN}" in ids
    assert f"ip:192.168.31.245" in ids
    assert all(KEY1 not in identity for identity in ids)
