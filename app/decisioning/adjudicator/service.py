"""Adjudicator: score -> policy -> decision, with engine invariants.

This is the decisioning layer's public service. It never mutates chassis state —
it returns Decision records (and ledgers them); the chassis/orchestrator executes
them. Boundary rule: this package imports domain + store + decisioning only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.decisioning.features.extract import extract_features
from app.decisioning.ledger.ledger import DecisionLedger
from app.decisioning.policy.engine import (PolicyPack, TierEvaluation,
                                           assert_automated_action_permitted)
from app.decisioning.scoring.model import MisuseModel
from app.domain.enums import (Actor, ClockState, ClockType, Cohort, DecisionAction,
                              NetworkCategory, PaymentChannel, RegRegime, Tier)
from app.domain.models import Account, Case, Claim, Customer, Decision, Transaction
from app.store.db import Store

CARD_CHANNELS = (PaymentChannel.CARD_CREDIT, PaymentChannel.CARD_DEBIT)


@dataclass
class TriageResult:
    decisions: list[Decision] = field(default_factory=list)
    evaluation: TierEvaluation | None = None
    score: float = 0.0
    reasons: list[dict] = field(default_factory=list)


class Adjudicator:
    def __init__(self, store: Store, ledger: DecisionLedger, pack: PolicyPack,
                 model: MisuseModel):
        self.store = store
        self.ledger = ledger
        self.pack = pack
        self.model = model
        self.live_scores: list[float] = []      # drift monitoring feed
        self._seq = 0                           # deterministic decision ids (replay)

    # ---------------- decision factory ----------------
    def _decision(self, now, case: Case, stage: str, action: DecisionAction,
                  rationale: str, actor: Actor = Actor.SYSTEM, rule_id: str = "",
                  score: float | None = None, reasons: list | None = None,
                  evaluation: TierEvaluation | None = None) -> Decision:
        if actor == Actor.SYSTEM:
            assert_automated_action_permitted(action)   # INV-NO-AUTO-DENIAL
        self._seq += 1
        d = Decision(
            id=f"D{self._seq:07d}", case_id=case.id, stage=stage,
            action=action, actor=actor,
            policy_pack_id=self.pack.id, policy_version=self.pack.version,
            rule_id=rule_id or (evaluation.rule.id if evaluation else ""),
            model_version=self.model.version if score is not None else None,
            score=score, reasons=reasons or [], rationale=rationale,
            cohort=case.cohort, decided_at=now)
        seq = self.ledger.append(now, case.id, f"DECISION:{action.value}", {
            "decision_id": d.id, "stage": stage, "actor": actor.value,
            "action": action.value, "rule_id": d.rule_id,
            "policy": f"{self.pack.id}@{self.pack.version}",
            "model_version": d.model_version, "score": score,
            "reasons": reasons or [], "rationale": rationale,
            "cohort": case.cohort.value if case.cohort else None,
            "trace": evaluation.trace if evaluation else []})
        d.ledger_seq = seq
        self.store.put_decision(d)
        return d

    # ---------------- stages ----------------
    def on_intake(self, now, case: Case, claim: Claim) -> list[Decision]:
        out = []
        if case.regime == RegRegime.REG_Z:
            out.append(self._decision(
                now, case, "INTAKE", DecisionAction.ACTIVATE_REG_Z_PENDENCY,
                "Reg Z pendency protections: disputed amount not payable, finance "
                "charges frozen, adverse credit reporting blocked during pendency "
                "(12 CFR 1026.13(d)).", rule_id="INV-REGZ-PENDENCY"))
        return out

    def on_triage(self, now, case: Case, claim: Claim, customer: Customer,
                  account: Account, txn: Transaction) -> TriageResult:
        feats = extract_features(self.store, claim, customer, account, txn)
        score = self.model.score(feats)
        reasons = self.model.reasons(feats)
        self.live_scores.append(score)
        case.misuse_score = score
        case.score_reasons = reasons
        case.cohort = case.cohort or self.pack.assign_cohort(case.id)

        evaluation = self.pack.evaluate(
            case.id, claim.amount, score, int(feats["claims_last_180d"]), case.cohort)
        case.tier = evaluation.tier
        case.policy_pack_id = f"{self.pack.id}@{self.pack.version}"

        res = TriageResult(evaluation=evaluation, score=score, reasons=reasons)
        rule = evaluation.rule

        if evaluation.tier == Tier.T0_AUTO_PAY:
            res.decisions.append(self._decision(
                now, case, "ADJUDICATION", DecisionAction.AUTO_PAY,
                f"Straight-through resolution in consumer favor under {rule.id}: "
                f"{rule.description}.", score=score, reasons=reasons,
                evaluation=evaluation))
            if self._recovery_eligible(claim, rule):
                res.decisions.append(self._decision(
                    now, case, "ADJUDICATION", DecisionAction.FILE_CHARGEBACK,
                    "Consumer resolution decoupled from recovery: amount above "
                    "recovery-economics threshold, chargeback filed post-payment.",
                    score=score, evaluation=evaluation))
        elif evaluation.tier == Tier.T1_PROVISIONAL_FAST:
            if case.regime == RegRegime.REG_E:
                res.decisions.append(self._decision(
                    now, case, "ADJUDICATION", DecisionAction.POST_PROVISIONAL_CREDIT,
                    "T1: provisional credit posted immediately; 45/90-day extended "
                    "window unlocked (12 CFR 1005.11(c)).",
                    score=score, reasons=reasons, evaluation=evaluation))
            res.decisions.append(self._decision(
                now, case, "ADJUDICATION", DecisionAction.ROUTE_STANDARD_INVESTIGATION,
                "T1: standard investigation.", score=score, evaluation=evaluation))
        else:  # T2 / T3
            candidate = evaluation.tier == Tier.T3_DEFENDED_DENIAL_CANDIDATE
            res.decisions.append(self._decision(
                now, case, "ADJUDICATION", DecisionAction.ROUTE_ENHANCED_INVESTIGATION,
                ("T3 defended-denial candidate: enhanced evidence gathering before "
                 "credit posture hardens; denial requires human review with "
                 "documented evidence." if candidate else
                 "T2: elevated misuse risk; enhanced evidence gathering."),
                score=score, reasons=reasons, evaluation=evaluation))
        return res

    def on_clock_event(self, now, case: Case, clock_type: ClockType,
                       transition: str) -> list[Decision]:
        """INV-PC-GUARANTEE: an at-risk Reg E investigate clock on an unresolved case
        with no PC forces provisional credit — as a ledgered policy decision, not a
        silent chassis side-effect."""
        out = []
        if (clock_type == ClockType.REG_E_INVESTIGATE
                and transition.endswith("AT_RISK")
                and case.regime == RegRegime.REG_E
                and case.resolved_at is None
                and case.provisional_credit.posted_at is None):
            out.append(self._decision(
                now, case, "CLOCK_GUARANTEE", DecisionAction.POST_PROVISIONAL_CREDIT,
                "Engine invariant INV-PC-GUARANTEE: investigate clock at risk with "
                "no determination; provisional credit posts now so the consumer is "
                "never made to wait past the regulatory deadline.",
                rule_id="INV-PC-GUARANTEE"))
        return out

    def human_action(self, now, case: Case, action: DecisionAction, rationale: str,
                     reviewer: str) -> Decision:
        """Human checkpoint actions (denial approval, pay approval, PC reversal)."""
        return self._decision(now, case, "HUMAN_REVIEW", action,
                              f"[{reviewer}] {rationale}", actor=Actor.HUMAN)

    # ---------------- helpers ----------------
    @staticmethod
    def _recovery_eligible(claim: Claim, rule) -> bool:
        return (rule.recovery == "FILE_CHARGEBACK_IF_CARD_AND_AMOUNT_GTE"
                and claim.channel in CARD_CHANNELS
                and claim.network_category is not None
                and claim.amount >= rule.recovery_min_amount)

    def drift_psi(self) -> float:
        return self.model.psi(self.live_scores)
