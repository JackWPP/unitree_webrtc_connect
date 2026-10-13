"""Reading the audit trail, and the bounded backward read that makes it cheap.

`tail()` used to call `readlines()` and slice. The handler rotates at 5 MB, so
answering "show me the last 50 operations" meant parsing tens of thousands of
lines — on every console poll and every `robot_audit_tail`. It now reads
backwards from the end.
"""

from __future__ import annotations

import json

from unitree_robot_agent.core.audit import AuditLog, _read_last_lines


def flush(log: AuditLog) -> None:
    for handler in log._logger.handlers:  # noqa: SLF001 - deliberate white-box
        handler.flush()


# ─── the bounded reader ───────────────────────────────────────────────


def test_it_returns_the_last_lines_in_order(tmp_path):
    path = tmp_path / "f.log"
    path.write_text("\n".join(f"line{i}" for i in range(1, 101)) + "\n", encoding="utf-8")

    assert _read_last_lines(path, 3) == ["line98", "line99", "line100"]


def test_it_handles_a_file_shorter_than_the_limit(tmp_path):
    path = tmp_path / "f.log"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    assert _read_last_lines(path, 50) == ["a", "b", "c"]


def test_it_handles_an_empty_file(tmp_path):
    path = tmp_path / "f.log"
    path.write_text("", encoding="utf-8")
    assert _read_last_lines(path, 5) == []


def test_it_handles_a_file_with_no_trailing_newline(tmp_path):
    path = tmp_path / "f.log"
    path.write_text("a\nb\nc", encoding="utf-8")
    assert _read_last_lines(path, 2) == ["b", "c"]


def test_it_reads_across_many_blocks(tmp_path):
    """The reader walks backwards in fixed blocks; a record longer than one
    block must not be truncated or split."""
    path = tmp_path / "f.log"
    long_line = "x" * 20000
    path.write_text(f"first\n{long_line}\nlast\n", encoding="utf-8")

    lines = _read_last_lines(path, 2, block=512)
    assert lines == [long_line, "last"]


def test_it_decodes_multibyte_characters_split_across_blocks(tmp_path):
    """Splitting on `b"\\n"` is byte-safe, but decoding must happen once at the
    end or a multi-byte character straddling a block boundary is mangled."""
    path = tmp_path / "f.log"
    payload = "机器狗" * 400
    path.write_text(f"head\n{payload}\ntail\n", encoding="utf-8")

    lines = _read_last_lines(path, 2, block=64)
    assert lines == [payload, "tail"]


# ─── the trail ────────────────────────────────────────────────────────


def test_tail_round_trips_records(tmp_path):
    log = AuditLog(path=tmp_path / "audit.log", robot_id="dog1")
    for index in range(50):
        log.record(f"tool{index}", ok=True)
    flush(log)

    records = log.tail(10)
    assert [r["tool"] for r in records] == [f"tool{i}" for i in range(40, 50)]
    assert all(r["robot_id"] == "dog1" for r in records)


def test_tail_falls_back_to_the_buffer_without_a_file(tmp_path):
    """A stdio deployment with no writable state dir still has a trail."""
    log = AuditLog(path=None, robot_id="dog1")
    log.record("robot_stop", ok=True)

    records = log.tail(5)
    assert [r["tool"] for r in records] == ["robot_stop"]


def test_tail_skips_a_corrupt_line_rather_than_failing(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path=path, robot_id="dog1")
    log.record("good_one", ok=True)
    flush(log)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    log.record("good_two", ok=True)
    flush(log)

    tools = [r["tool"] for r in log.tail(10)]
    assert tools == ["good_one", "good_two"]


def test_tail_clamps_its_limit(tmp_path):
    log = AuditLog(path=tmp_path / "audit.log", robot_id="dog1")
    for index in range(5):
        log.record(f"t{index}", ok=True)
    flush(log)

    assert len(log.tail(0)) == 1        # clamped up
    assert len(log.tail(10_000)) == 5   # clamped down to what exists


def test_the_trail_redacts_secrets_on_write(tmp_path):
    """The console serves this file over HTTP, so redaction has to happen here
    rather than at the point of display."""
    log = AuditLog(path=tmp_path / "audit.log", robot_id="dog1")
    log.record(
        "robot_fetch_key",
        args={"aes_128_key": "deadbeef" * 4, "password": "hunter2"},
        ok=True,
    )
    flush(log)

    serialised = json.dumps(log.tail(5))
    assert "deadbeef" not in serialised
    assert "hunter2" not in serialised
