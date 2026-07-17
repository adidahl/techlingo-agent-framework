"""Test vectors for MASTERY_SPEC v1 + SESSION_SPEC v1 (learning_specs.py).

These vectors are NORMATIVE: the TechLingo app's TypeScript implementation
must reproduce them exactly (see TECHLINGO_APP_LEARNING_ENGINE_TASK.md).
Change them only together with a spec version bump.

Run without pytest:  PYTHONPATH=src python tests/test_learning_specs.py
"""

from __future__ import annotations

from techlingo_workflow.learning_specs import (
    ConceptMastery,
    PoolItem,
    UserConceptState,
    apply_answer,
    compose_session,
    effective_strength,
    is_due,
)


# ---------------------------------------------------------------------------
# MASTERY vectors
# ---------------------------------------------------------------------------


def _climb_ladder() -> ConceptMastery:
    m = ConceptMastery()
    for rung in (1, 2, 3, 4, 5):
        m = apply_answer(m, rung=rung, correct=True, now_days=0.0)
    return m


def test_v1_ladder_climb_day0():
    m = ConceptMastery()
    expected = [
        (1, 0.15, 1.0),
        (2, 0.32, 1.8),
        (3, 0.524, 3.24),
        (4, 0.6906, 5.832),
        (5, 0.81436, 10.4976),
    ]
    for rung, strength, half_life in expected:
        m = apply_answer(m, rung=rung, correct=True, now_days=0.0)
        assert (m.strength, m.half_life_days) == (strength, half_life), f"R{rung}"


def test_v2_decay_and_due():
    m = _climb_ladder()
    expected = [
        (0.0, 0.81436, False),
        (3.0, 0.668018, False),
        (7.0, 0.51296, True),
        (10.4976, 0.40718, True),   # exactly one half-life -> exactly half
        (21.0, 0.203525, True),
        (60.0, 0.015497, True),
    ]
    for day, eff, due in expected:
        assert effective_strength(m, day) == eff, f"day {day}"
        assert is_due(m, day) is due, f"day {day}"


def test_v3_wrong_then_recover():
    m = _climb_ladder()
    m = apply_answer(m, rung=2, correct=False, now_days=7.0)
    assert (m.strength, m.half_life_days) == (0.25648, 5.2488)
    m = apply_answer(m, rung=3, correct=True, now_days=8.0)
    assert (m.strength, m.half_life_days) == (0.457326, 9.44784)


def test_v4_first_answer_wrong():
    m = apply_answer(ConceptMastery(), rung=1, correct=False, now_days=0.0)
    assert (m.strength, m.half_life_days) == (0.0, 0.5)
    assert effective_strength(m, 0.0) == 0.0
    assert is_due(m, 0.0) is True  # seen + effectively zero -> due immediately


def test_v5_update_applies_to_decayed_strength():
    m = _climb_ladder()
    assert effective_strength(m, 30.0) == 0.11234  # what's actually left after 30 days
    m = apply_answer(m, rung=1, correct=True, now_days=30.0)
    assert (m.strength, m.half_life_days) == (0.245489, 18.89568)


def test_v6_half_life_caps():
    m = ConceptMastery(strength=0.9, half_life_days=80.0, last_review_at=0.0)
    m = apply_answer(m, rung=5, correct=True, now_days=1.0)
    assert m.half_life_days == 90.0  # growth capped
    m2 = ConceptMastery(strength=0.4, half_life_days=0.8, last_review_at=0.0)
    m2 = apply_answer(m2, rung=1, correct=False, now_days=0.0)
    assert m2.half_life_days == 0.5  # shrink floored


# ---------------------------------------------------------------------------
# SESSION vector — one deterministic composition scenario
# ---------------------------------------------------------------------------


def _pool() -> list[PoolItem]:
    """3 concepts (curriculum order: alpha, beta, gamma), small realistic pool."""
    return [
        PoolItem("l1/alpha/r1/v1", "alpha", 1, seen=True),
        PoolItem("l1/alpha/r2/v1", "alpha", 2, seen=True),
        PoolItem("l1/alpha/r3/v1", "alpha", 3, seen=False),
        PoolItem("l1/beta/r1/v1", "beta", 1, seen=True),
        PoolItem("l1/beta/r1/v2", "beta", 1, seen=False),
        PoolItem("l1/beta/r2/v1", "beta", 2, seen=False),
        PoolItem("l2/gamma/r1/v1", "gamma", 1, seen=False),
        PoolItem("l2/gamma/r2/v1", "gamma", 2, seen=False),
        PoolItem("l2/gamma/r4/v1", "gamma", 4, seen=False),
    ]


def _user_state() -> dict[str, UserConceptState]:
    # alpha: strong (answered today, high strength) -> warm-up source
    alpha = UserConceptState(
        "alpha", ConceptMastery(strength=0.81436, half_life_days=10.4976, last_review_at=10.0), proven_rung=2
    )
    # beta: due (last answered 10 days ago, short half-life) -> review source
    beta = UserConceptState(
        "beta", ConceptMastery(strength=0.32, half_life_days=1.8, last_review_at=0.0), proven_rung=1
    )
    # gamma: never seen -> new source
    return {"alpha": alpha, "beta": beta, "gamma": UserConceptState("gamma")}


def test_session_vector_composition():
    plan = compose_session(
        _pool(), _user_state(), mistake_queue=["l1/alpha/r2/v1"], now_days=10.0, session_size=6
    )
    # warm-up: alpha is the only strong concept; its lowest-rung item wins.
    assert plan.warmup == ["l1/alpha/r1/v1"]
    # buckets (size 6 -> 5 after warm-up): mistakes floor(5*0.15)=0 ... but the
    # queue holds 1 item -> min(1, 0) = 0 mistakes; review floor(5*0.25)=1;
    # new = 4.
    assert plan.mistakes == []
    # beta is due (eff = 0.32 * 2^(-10/1.8) = 0.006858 < 0.55). proven_rung=1 ->
    # target rung 1; unseen variant of the same cell preferred: beta/r1/v2.
    assert plan.review == ["l1/beta/r1/v2"]
    # new: curriculum order, next-unproven-rung gate, max 2/concept over the
    # whole session. Strict pass takes alpha r3 (proven 2 -> next 3), beta r2
    # (proven 1 -> next 2), gamma r1 (unseen -> next 1); gamma r2 is beyond the
    # gate, so the strict pass stops at 3 of 4 slots. The SHORTFALL pass then
    # relaxes the rung gate (small pools must still fill a session) and takes
    # gamma r2 — acceptable because gamma r1 plays EARLIER in the same session
    # (rung-ascending order) and the 2-per-concept cap still holds.
    assert plan.new == [
        "l1/alpha/r3/v1",
        "l1/beta/r2/v1",
        "l2/gamma/r1/v1",
        "l2/gamma/r2/v1",
    ]
    # final order: warm-up pinned, then rung-ascending; same-concept adjacency
    # resolved by swapping ahead (beta r1 -> gamma r1 -> beta r2 -> gamma r2).
    assert plan.final_order == [
        "l1/alpha/r1/v1",
        "l1/beta/r1/v2",
        "l2/gamma/r1/v1",
        "l1/beta/r2/v1",
        "l2/gamma/r2/v1",
        "l1/alpha/r3/v1",
    ]


def test_session_max_two_per_concept_and_size():
    # Large session over a tiny pool: caps must hold, session may come up short.
    plan = compose_session(_pool(), _user_state(), mistake_queue=[], now_days=10.0, session_size=12)
    from collections import Counter

    counts = Counter(key.split("/")[1] for key in plan.final_order)
    assert all(c <= 2 for c in counts.values()), counts
    assert len(plan.final_order) == len(set(plan.final_order))


def test_session_mistakes_bucket_when_share_allows():
    plan = compose_session(
        _pool(), _user_state(), mistake_queue=["l2/gamma/r2/v1"], now_days=10.0, session_size=12
    )
    # size 12 -> 11 body slots -> mistakes floor(11*0.15)=1.
    assert plan.mistakes == ["l2/gamma/r2/v1"]


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(1 if _run_all() else 0)
