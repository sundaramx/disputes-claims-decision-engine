"""End-to-end property tests: run the full virtual-time simulation, then assert the
engine's non-negotiable invariants over EVERY case it produced (PLAN.md §12, §15)."""
import pytest

from app.decisioning.learning.metrics import compute_kpis
from app.domain.enums import (Actor, CaseStatus, ClockType, DecisionAction,
                              PaymentChannel, RegRegime)
from app.simulation.lifecycle import run_simulation


@pytest.fixture(scope="module")
def sim():
    store, engine, portfolio = run_simulation(seed=11, n_customers=250,
                                              n_claims=300, horizon_days=80)
    return store, engine


def _clock(case, ct):
    for k in case.clocks:
        if k.clock_type == ct:
            return k
    return None


def test_no_automated_denial_ever(sim):
    store, _ = sim
    for d in store.all_decisions():
        if d.actor == Actor.SYSTEM:
            assert d.action not in (DecisionAction.APPROVE_DENIAL,
                                    DecisionAction.REVERSE_PROVISIONAL_CREDIT), \
                f"automated forbidden action on {d.case_id}"


def test_every_denial_is_human_approved(sim):
    store, _ = sim
    for case in store.all_cases():
        if case.status == CaseStatus.RESOLVED_DENIED:
            denials = [d for d in store.decisions_for_case(case.id)
                       if d.action == DecisionAction.APPROVE_DENIAL]
            assert denials and all(d.actor == Actor.HUMAN for d in denials)


def test_pc_guarantee_all_cohorts(sim):
    """Any Reg E case unresolved at its investigate deadline must have PC posted by
    that deadline — champion and challenger alike (INV-PC-GUARANTEE)."""
    store, engine = sim
    now = engine.clock.now()
    for case in store.all_cases():
        if case.regime != RegRegime.REG_E:
            continue
        inv = _clock(case, ClockType.REG_E_INVESTIGATE)
        # determination time is what the investigate clock governs; a post-
        # determination reopen does not retroactively violate it
        undetermined_past_due = ((case.determined_at or now) > inv.due_at)
        if undetermined_past_due:
            pc = case.provisional_credit
            assert pc.posted_at is not None and pc.posted_at <= inv.due_at, \
                f"{case.id} ({case.cohort}) past investigate deadline without timely PC"


def test_reg_z_never_gets_pc_and_always_gets_pendency(sim):
    store, _ = sim
    for case in store.all_cases():
        if case.regime == RegRegime.REG_Z:
            assert case.provisional_credit.posted_at is None, case.id
            assert case.reg_z_pendency.activated_at is not None, case.id
            assert case.reg_z_pendency.finance_charge_freeze
            assert case.reg_z_pendency.adverse_reporting_block


def test_zelle_ach_have_no_representment(sim):
    store, _ = sim
    for case in store.all_cases():
        claim = store.get_claim(case.claim_id)
        if claim.channel in (PaymentChannel.ZELLE, PaymentChannel.ACH):
            o = store.get_outcome(case.id)
            if o:
                assert not o.representment_filed, case.id
            assert claim.network_category is None


def test_every_decision_is_ledgered_with_versions(sim):
    store, _ = sim
    ledgered = {e.payload.get("decision_id")
                for e in store.ledger_entries() if e.event_type.startswith("DECISION:")}
    for d in store.all_decisions():
        assert d.id in ledgered, f"decision {d.id} missing from ledger"
        assert d.policy_pack_id and d.policy_version, d.id
        if d.score is not None:
            assert d.model_version, d.id


def test_breached_cases_escalate(sim):
    store, _ = sim
    from app.domain.enums import ClockState
    for case in store.all_cases():
        if any(k.state == ClockState.BREACHED and
               k.clock_type.value.startswith("REG_") for k in case.clocks):
            assert case.escalated, case.id


def test_ledger_chain_valid_over_full_run(sim):
    _, engine = sim
    report = engine.ledger.verify()
    assert report["valid"] and report["entries"] > 1000


def test_kpis_computable_and_sane(sim):
    store, engine = sim
    k = compute_kpis(store, engine.model, engine.adjudicator.live_scores,
                     engine.clock.now())
    assert k["compliance"]["pc_on_time_rate"] == 100.0
    assert k["compliance"]["reg_z_pendency_activation_rate"] == 100.0
    assert 0 < k["efficiency"]["stp_rate"] < 100
    assert k["model"]["live_auc_on_closed_cases"] > 0.8
    assert set(k["experiments"]["cohorts"]) == {"CHAMPION", "CHALLENGER"}


def test_deterministic_replay_same_seed_same_ledger_head():
    _, e1, _ = run_simulation(seed=99, n_customers=120, n_claims=120, horizon_days=60)
    _, e2, _ = run_simulation(seed=99, n_customers=120, n_claims=120, horizon_days=60)
    r1, r2 = e1.ledger.verify(), e2.ledger.verify()
    assert r1["entries"] == r2["entries"]
    assert r1["head_hash"] == r2["head_hash"]
