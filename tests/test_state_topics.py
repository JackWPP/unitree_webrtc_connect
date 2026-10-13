"""Reading the robot's state when the firmware picks which topic to publish on.

Two Unitree topics carry the same thing: `rt/lf/sportmodestate` and
`rt/sportmodestate`. Which one a robot answers on depends on its firmware, not
its model — the R1 profile has always used the `lf` one while the Go2 profile
used the other, and a real Go2 (B42N6000Q3UC1M8C, firmware requiring `data2=3`)
answered neither for eight seconds when only `rt/sportmodestate` was tried.

That finding is what this fixes. The sibling project `unitree_harness` prefers
`rt/lf/sportmodestate` and falls back the other way, which is a strong hint the
first attempt was simply the wrong topic.
"""

from __future__ import annotations

import pytest

from unitree_robot_agent.core.telemetry import GO2_STATE_TOPICS, first_topic
from unitree_robot_agent.robots.go2 import Go2Profile
from unitree_robot_agent.robots.r1 import R1Profile


class FakeSession:
    """Answers only the topics it is told to."""

    def __init__(self, answering, payload=None):
        self.answering = set(answering)
        self.payload = payload if payload is not None else {"mode": 0}
        self.asked: list = []

    async def read_topic(self, topic, **kwargs):
        self.asked.append(topic)
        return self.payload if topic in self.answering else None


# ─── the profile's candidate list ─────────────────────────────────────


def test_the_go2_tries_the_low_frequency_topic_first():
    """`unitree_harness` prefers it, and its author has run this hardware far
    more than this repo has."""
    assert Go2Profile().state_candidates == (
        "rt/lf/sportmodestate",
        "rt/sportmodestate",
    )


def test_the_r1_keeps_its_single_topic():
    assert R1Profile().state_candidates == ("rt/lf/sportmodestate",)


def test_candidates_are_deduplicated_and_never_contain_none():
    class Profile(Go2Profile):
        state_topics = ("rt/a", "rt/a", "", None, "rt/b")

    assert Profile().state_candidates == ("rt/a", "rt/b")


def test_a_profile_with_no_topics_answers_nothing():
    class Profile(Go2Profile):
        state_topic = None
        state_topics = ()

    assert Profile().state_candidates == ()


def test_the_declared_read_budget_covers_every_candidate():
    """The console's ceiling is computed from `GO2_STATE_TOPICS`. If the Go2
    starts trying more topics than that, the budget is fiction."""
    assert len(Go2Profile().state_candidates) <= GO2_STATE_TOPICS


# ─── the read ─────────────────────────────────────────────────────────


async def test_it_stops_at_the_first_topic_that_answers():
    session = FakeSession({"rt/lf/sportmodestate", "rt/sportmodestate"})
    topic, payload = await first_topic(
        session, ("rt/lf/sportmodestate", "rt/sportmodestate"), timeout=0.01
    )

    assert topic == "rt/lf/sportmodestate"
    assert payload is not None
    assert session.asked == ["rt/lf/sportmodestate"], "it read both anyway"


async def test_it_falls_back_when_the_first_is_silent():
    """The real-hardware case: the preferred topic says nothing, the other one
    has the data."""
    session = FakeSession({"rt/sportmodestate"})
    topic, payload = await first_topic(
        session, ("rt/lf/sportmodestate", "rt/sportmodestate"), timeout=0.01
    )

    assert topic == "rt/sportmodestate"
    assert payload is not None
    assert session.asked == ["rt/lf/sportmodestate", "rt/sportmodestate"]


async def test_both_silent_is_none_rather_than_an_exception():
    session = FakeSession(set())
    topic, payload = await first_topic(session, ("rt/a", "rt/b"), timeout=0.01)

    assert topic is None and payload is None
    assert session.asked == ["rt/a", "rt/b"]


async def test_an_empty_candidate_list_does_not_read_anything():
    session = FakeSession({"rt/a"})
    topic, payload = await first_topic(session, (), timeout=0.01)

    assert topic is None and payload is None
    assert session.asked == []


# ─── merging, because the topics are complementary ────────────────────


async def test_it_merges_rather_than_taking_the_first_answer():
    """The finding that made `first_topic` the wrong primitive here.

    `rt/lf/sportmodestate` and `rt/sportmodestate` are not two names for the
    same payload: the first carries `fsm_id` (what a humanoid reads) and the
    second carries `gait_type` (what a quadruped reads). Taking whichever
    answers first silently drops the other's fields.
    """
    from unitree_robot_agent.core.telemetry import merge_topics

    class Split:
        async def read_topic(self, topic, **kwargs):
            if topic == "rt/lf/sportmodestate":
                return {"fsm_id": 4, "mode": 1}
            if topic == "rt/sportmodestate":
                return {"gait_type": "trot", "mode": 2}
            return None

    merged = await merge_topics(
        Split(), ("rt/lf/sportmodestate", "rt/sportmodestate"), timeout=0.01
    )

    assert merged["fsm_id"] == 4, "the humanoid field was lost"
    assert merged["gait_type"] == "trot", "the quadruped field was lost"
    # Earlier topics win a conflict, so the preferred source defines `mode`.
    assert merged["mode"] == 1


async def test_merging_survives_a_silent_candidate():
    from unitree_robot_agent.core.telemetry import merge_topics

    session = FakeSession({"rt/sportmodestate"}, payload={"gait_type": "trot"})
    merged = await merge_topics(
        session, ("rt/lf/sportmodestate", "rt/sportmodestate"), timeout=0.01
    )

    assert merged == {"gait_type": "trot"}
    assert session.asked == ["rt/lf/sportmodestate", "rt/sportmodestate"]


async def test_merging_everything_silent_is_none():
    from unitree_robot_agent.core.telemetry import merge_topics

    assert await merge_topics(FakeSession(set()), ("rt/a", "rt/b"), timeout=0.01) is None
