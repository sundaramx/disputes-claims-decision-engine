"""Policy engine, invariants, and ledger integrity tests."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.decisioning.ledger.ledger import DecisionLedger
from app.decisioning.policy.engine import (PolicyPack, PolicyViolation,
                                           assert_automated_action_permitted)
from app.domain.enums import Cohort, DecisionAction, Tier
from app.store.db import Store

PACK = Path(__file__).resolve().parents[1] / "policies" / "adjudication_v1.yaml"
UTC = timezone.utc


def test_tier_ladder_champion():
    pack = PolicyPack(PACK)
    ev = pack.evaluate("case-x", amount=50, misuse_score=0.1, claims_180d=0,
                       cohort=Cohort.CHAMPION)
    assert ev.tier == Tier.T0_AUTO_PAY
    ev = pack.evaluate("case-x", amount=50, misuse_score=0.1, claims_180d=3,
                       cohort=Cohort.CHAMPION)
    assert ev.tier == Tier.T1_PROVISIONAL_FAST      # serial disputer blocks T0
    ev = pack.evaluate("case-x", amount=500, misuse_score=0.7, claims_180d=0,
                       cohort=Cohort.CHAMPION)
    assert ev.tier == Tier.T2_ENHANCED_INVESTIGATION
    ev = pack.evaluate("case-x", amount=500, misuse_score=0.95, claims_180d=0,
                       cohort=Cohort.CHAMPION)
    assert ev.tier == Tier.T3_DEFENDED_DENIAL_CANDIDATE
    assert any("MATCH" in line for line in ev.trace)   # rule trace present


def test_challenger_wider_t0_band():
    pack = PolicyPack(PACK)
    champ = pack.evaluate("c", 120, 0.30, 0, cohort=Cohort.CHAMPION)
    chall = pack.evaluate("c", 120, 0.30, 0, cohort=Cohort.CHALLENGER)
    assert champ.tier == Tier.T1_PROVISIONAL_FAST
    assert chall.tier == Tier.T0_AUTO_PAY


def test_cohort_assignment_deterministic_and_split():
    pack = PolicyPack(PACK)
    ids = [f"case-{i}" for i in range(2000)]
    a = [pack.assign_cohort(i) for i in ids]
    b = [pack.assign_cohort(i) for i in ids]
    assert a == b
    share = sum(1 for c in a if c == Cohort.CHALLENGER) / len(a)
    assert 0.15 < share < 0.25


def test_no_auto_denial_invariant():
    with pytest.raises(PolicyViolation):
        assert_automated_action_permitted(DecisionAction.APPROVE_DENIAL)
    with pytest.raises(PolicyViolation):
        assert_automated_action_permitted(DecisionAction.REVERSE_PROVISIONAL_CREDIT)
    assert_automated_action_permitted(DecisionAction.AUTO_PAY)  # allowed


def test_malicious_policy_pack_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("""
id: evil
version: "0.1"
challenger_share: 0
cohorts:
  CHAMPION:
    tiers:
      - id: T0_AUTO_PAY
        max_misuse_score: 1.01
        resolve_consumer: AUTO_DENY
""")
    with pytest.raises(PolicyViolation):
        PolicyPack(bad)


# ---------------- ledger ----------------

def test_ledger_chain_verifies_and_detects_tamper():
    store = Store(":memory:")
    ledger = DecisionLedger(store)
    now = datetime(2026, 1, 5, tzinfo=UTC)
    for i in range(25):
        ledger.append(now, f"case-{i % 3}", "DECISION:AUTO_PAY",
                      {"n": i, "rationale": "test"})
    report = ledger.verify()
    assert report["valid"] and report["entries"] == 25

    # tamper with a mid-chain payload directly in SQLite
    store._conn.execute("UPDATE ledger SET payload='{\"n\":999}' WHERE seq=10")
    store._conn.commit()
    report = ledger.verify()
    assert not report["valid"]
    assert report["broken_at_seq"] == 10


def test_ledger_reconstructable_from_row_alone():
    store = Store(":memory:")
    ledger = DecisionLedger(store)
    now = datetime(2026, 1, 5, tzinfo=UTC)
    payload = {"action": "AUTO_PAY", "score": 0.12, "policy": "adjudication@1.2.0",
               "rationale": "straight-through under T0"}
    ledger.append(now, "case-1", "DECISION:AUTO_PAY", payload)
    [entry] = store.ledger_entries("case-1")
    # the full payload is in the ledger row itself, not a foreign table
    assert entry.payload["policy"] == "adjudication@1.2.0"
    assert entry.payload["rationale"].startswith("straight-through")
