"""DisputeEngine — the orchestrator that wires chassis <-> decisioning.

This is the ONLY module (besides the API) that imports both layers. The chassis
executes and reports; the decisioning layer scores, decides, and ledgers; the
engine moves events between them and ledgers chassis-side events so the ledger
is the complete case history (PLAN.md §15.10).
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.chassis.workflow import CaseChassis, ChassisEvent
from app.decisioning.adjudicator.service import Adjudicator
from app.decisioning.evidence.orchestrator import EvidenceBundle, EvidenceOrchestrator
from app.decisioning.ledger.ledger import DecisionLedger
from app.decisioning.policy.engine import PolicyPack
from app.decisioning.scoring.model import MisuseModel
from app.domain.clock import SimClock
from app.domain.enums import (CaseStatus, DecisionAction, NetworkCategory,
                              PaymentChannel, RegRegime, Tier, TrueType)
from app.domain.models import Case, Claim, Inquiry, Outcome
from app.network.adapters import MockNetworkBackend
from app.simulation.generator import ClaimPlan, Portfolio
from app.store.db import Store

CARD = (PaymentChannel.CARD_CREDIT, PaymentChannel.CARD_DEBIT)

# representment win probabilities by (network category, whether claim is truly
# first-party). The world's behavior, seeded per case for deterministic replay.
REPRESENTMENT_WIN = {
    NetworkCategory.PROCESSING_ERROR: 0.75,
    NetworkCategory.CONSUMER_DISPUTE: 0.45,
    NetworkCategory.FRAUD_CP: 0.40,
    NetworkCategory.FRAUD_CNP: 0.45,
}
CE3_COUNTEREVIDENCE_WIN = 0.08   # we filed fraud cb but merchant holds CE3 evidence

STANDARD_CAPACITY_PER_DAY = 30
ENHANCED_CAPACITY_PER_DAY = 12
REVIEW_CAPACITY_PER_DAY = 10
STUCK_REVIEW_PROB = 0.06         # simulates analyst unavailability -> breach source


@dataclass
class WorkItem:
    case_id: str
    ready_day: int
    bundle: EvidenceBundle | None = None


@dataclass
class KContext:
    """Per-case simulation context the engine tracks during a run."""
    plan: ClaimPlan
    rng: random.Random
    representment_due_day: int | None = None


class DisputeEngine:
    def __init__(self, store: Store, clock: SimClock, pack: PolicyPack,
                 model: MisuseModel, participation: dict[str, bool]):
        self.store = store
        self.clock = clock
        self.ledger = DecisionLedger(store)
        self.chassis = CaseChassis(store)
        self.network = MockNetworkBackend(participation)
        self.adjudicator = Adjudicator(store, self.ledger, pack, model)
        self.evidence = EvidenceOrchestrator(store, self.network)
        self.pack = pack
        self.model = model
        self.day = 0
        self.contexts: dict[str, KContext] = {}
        self.std_queue: list[WorkItem] = []
        self.enh_queue: list[WorkItem] = []
        self.review_queue: list[WorkItem] = []
        self.reopen_queue: list[WorkItem] = []

    # ------------------------------------------------------------------ intake
    def process_arrival(self, plan: ClaimPlan) -> None:
        """Pre-claim inquiry (deflection surface) or direct claim filing."""
        now = self.clock.now()
        claim = plan.claim
        rng = random.Random(f"ARR-{claim.id}")
        if plan.starts_as_inquiry:
            txn = self.store.get_transaction(claim.transaction_id)
            oi = self.network.order_insight_lookup(claim, txn, plan.evidence_quality)
            recognized = oi.participating and oi.order_found and (
                oi.device_match or oi.delivery_confirmation
                or oi.prior_undisputed_orders >= 2)
            deflected = recognized and rng.random() < 0.55
            inq = Inquiry(id=f"IQ{claim.id[2:]}", customer_id=claim.customer_id,
                          transaction_id=claim.transaction_id, occurred_at=now,
                          order_insight_hit=oi.participating and oi.order_found,
                          deflected=deflected,
                          converted_claim_id=None if deflected else claim.id)
            self.store.put_inquiry(inq)
            if deflected:
                return   # cheapest dispute: the one that never becomes a claim
        self.file_claim(plan)

    def file_claim(self, plan: ClaimPlan) -> Case:
        now = self.clock.now()
        claim = plan.claim
        self.store.put_claim(claim)
        account = self.store.get_account(claim.account_id)
        customer = self.store.get_customer(claim.customer_id)
        txn = self.store.get_transaction(claim.transaction_id)

        case, events = self.chassis.open_case(claim, account, txn, now)
        self._ledger_chassis(events, now)
        ctx = KContext(plan=plan, rng=random.Random(f"CTX-{case.id}"))
        self.contexts[case.id] = ctx

        for d in self.adjudicator.on_intake(now, case, claim):
            self._execute(d, case, claim, account)
        result = self.adjudicator.on_triage(now, case, claim, customer, account, txn)
        for d in result.decisions:
            self._execute(d, case, claim, account)

        # route follow-up work into the simulated back office
        if case.status == CaseStatus.INVESTIGATION:
            dwell = ctx.rng.randint(2, 8)
            item = WorkItem(case.id, self.day + dwell)
            (self.enh_queue if case.assigned_queue == "INVESTIGATION_ENHANCED"
             else self.std_queue).append(item)
        if case.status == CaseStatus.RESOLVED_PAID:
            self._maybe_close(case, claim)
        self.store.put_case(case)
        return case

    # ------------------------------------------------------------------ daily
    def daily_tick(self) -> None:
        """One virtual day: clocks -> guarantees -> back-office work -> network."""
        now = self.clock.now()
        # 1. clocks & invariant guarantees
        for case in self.store.cases(status=None):
            if case.resolved_at is not None:
                continue
            claim = self.store.get_claim(case.claim_id)
            account = self.store.get_account(case.account_id)
            for e in self.chassis.refresh_clocks(case, now):
                self.ledger.append(now, case.id, f"CLOCK:{e.transition}", {
                    "clock": e.clock_type.value, "due_at": e.due_at.isoformat(),
                    "escalated": case.escalated})
                for d in self.adjudicator.on_clock_event(now, case, e.clock_type,
                                                         e.transition):
                    self._execute(d, case, claim, account)
            self.store.put_case(case)

        # 2. investigations (capacity-bound)
        self._work_investigations(self.std_queue, STANDARD_CAPACITY_PER_DAY)
        self._work_investigations(self.enh_queue, ENHANCED_CAPACITY_PER_DAY)

        # 3. human review checkpoint (capacity-bound)
        self._work_reviews()

        # 4. reopens and merchant representment responses
        self._work_reopens()
        self._work_representments()
        self.day += 1

    # ------------------------------------------------------------ back office
    def _work_investigations(self, queue: list[WorkItem], capacity: int) -> None:
        now = self.clock.now()
        ready = [w for w in queue if w.ready_day <= self.day][:capacity]
        for w in ready:
            queue.remove(w)
            case = self.store.get_case(w.case_id)
            if case.resolved_at is not None:
                continue
            claim = self.store.get_claim(case.claim_id)
            account = self.store.get_account(case.account_id)
            txn = self.store.get_transaction(claim.transaction_id)
            ctx = self.contexts[case.id]
            case.touches += 1

            bundle = self.evidence.investigate(now, case, claim, txn,
                                               ctx.plan.evidence_quality)
            self.ledger.append(now, case.id, "EVIDENCE_BUNDLE", {
                "items": [{"source": i.source, "summary": i.summary}
                          for i in bundle.items],
                "ce3_qualified": bundle.ce3_qualified,
                "recommendation": bundle.recommendation,
                "confidence": bundle.confidence,
                "narrative_draft": bundle.narrative})

            if bundle.recommendation == "CONFIRM_FIRST_PARTY":
                d = self.adjudicator._decision(
                    now, case, "INVESTIGATION", DecisionAction.ROUTE_HUMAN_REVIEW,
                    f"Evidence supports first-party misuse "
                    f"(confidence {bundle.confidence:.2f}); denial requires human "
                    f"review with documented evidence.", rule_id="EVIDENCE-CHECKPOINT")
                self._execute(d, case, claim, account)
                dwell = ctx.rng.randint(1, 3)
                if ctx.rng.random() < STUCK_REVIEW_PROB:
                    # analyst unavailability tail: long enough that some cases blow
                    # the 45-day resolve window despite timely PC — breach detection
                    # and escalation must be shown working, not just asserted
                    dwell += ctx.rng.randint(30, 55)
                self.review_queue.append(WorkItem(case.id, self.day + dwell, bundle))
            else:
                # consumer-favor resolution (final pay), then recovery evaluation
                d = self.adjudicator._decision(
                    now, case, "INVESTIGATION", DecisionAction.AUTO_PAY,
                    ("Evidence supports the consumer." if
                     bundle.recommendation == "SUPPORT_CONSUMER" else
                     "Evidence inconclusive; Reg E/Z default resolves in the "
                     "consumer's favor."), rule_id="EVIDENCE-RESOLVE")
                self._execute(d, case, claim, account)
                if (claim.channel in CARD and claim.network_category and
                        claim.amount >= 25 and
                        bundle.recommendation == "SUPPORT_CONSUMER"):
                    d2 = self.adjudicator._decision(
                        now, case, "RECOVERY", DecisionAction.FILE_CHARGEBACK,
                        "Recovery evaluation: evidence supports chargeback.",
                        rule_id="RECOVERY-EVAL")
                    self._execute(d2, case, claim, account)
                self._maybe_close(case, claim)
            self.store.put_case(case)

    def _work_reviews(self) -> None:
        now = self.clock.now()
        ready = [w for w in self.review_queue
                 if w.ready_day <= self.day][:REVIEW_CAPACITY_PER_DAY]
        for w in ready:
            self.review_queue.remove(w)
            case = self.store.get_case(w.case_id)
            if case.resolved_at is not None:
                continue
            claim = self.store.get_claim(case.claim_id)
            account = self.store.get_account(case.account_id)
            ctx = self.contexts[case.id]
            case.touches += 1
            bundle = w.bundle
            # the human agrees with well-documented evidence most of the time
            if bundle and bundle.confidence >= 0.7 and ctx.rng.random() < 0.85:
                d = self.adjudicator.human_action(
                    now, case, DecisionAction.APPROVE_DENIAL,
                    f"Denial approved on documented evidence "
                    f"(recommendation {bundle.recommendation}, confidence "
                    f"{bundle.confidence:.2f}; CE3 qualified={bundle.ce3_qualified}).",
                    reviewer="analyst.q1")
            else:
                d = self.adjudicator.human_action(
                    now, case, DecisionAction.APPROVE_PAY,
                    "Evidence insufficient to support denial; resolving in "
                    "consumer favor.", reviewer="analyst.q1")
            self._execute(d, case, claim, account)
            self._maybe_close(case, claim)

    def _work_reopens(self) -> None:
        now = self.clock.now()
        for w in [w for w in self.reopen_queue if w.ready_day <= self.day]:
            self.reopen_queue.remove(w)
            case = self.store.get_case(w.case_id)
            claim = self.store.get_claim(case.claim_id)
            account = self.store.get_account(case.account_id)
            case.touches += 1
            d = self.adjudicator.human_action(
                now, case, DecisionAction.APPROVE_PAY,
                "Reopened after customer escalation; determination overturned.",
                reviewer="analyst.sup")
            case.resolved_at = None      # transiently reopened
            self._execute(d, case, claim, account)
            o = self.store.get_outcome(case.id)
            if o:
                o.resolution, o.liability = "PAID", "ISSUER"
                o.loss_amount, o.reopened = claim.amount, True
                o.closed_at = now
                self.store.put_outcome(o)
                self.ledger.append(now, case.id, "OUTCOME_UPDATED",
                                   {"reopened": True, "resolution": "PAID"})

    def _work_representments(self) -> None:
        now = self.clock.now()
        for case_id, ctx in list(self.contexts.items()):
            if ctx.representment_due_day is None or ctx.representment_due_day > self.day:
                continue
            ctx.representment_due_day = None
            case = self.store.get_case(case_id)
            claim = self.store.get_claim(case.claim_id)
            o = self.store.get_outcome(case_id)
            if o is None or not o.representment_filed:
                continue
            truly_fp = claim.true_type == TrueType.FIRST_PARTY_MISUSE
            base = REPRESENTMENT_WIN.get(claim.network_category, 0.4)
            win_p = CE3_COUNTEREVIDENCE_WIN if (
                truly_fp and claim.network_category == NetworkCategory.FRAUD_CNP) \
                else base
            won = ctx.rng.random() < win_p
            o.representment_won = won
            if won:
                o.recovered_amount = claim.amount
                o.loss_amount = max(0.0, o.loss_amount - claim.amount)
                o.liability = "MERCHANT"
            self.store.put_outcome(o)
            self.ledger.append(now, case_id, "REPRESENTMENT_RESULT", {
                "won": won, "category":
                claim.network_category.value if claim.network_category else None,
                "recovered": o.recovered_amount})

    # ------------------------------------------------------------- outcomes
    def _maybe_close(self, case: Case, claim: Claim) -> None:
        now = self.clock.now()
        if case.resolved_at is None or self.store.get_outcome(case.id):
            return
        ctx = self.contexts[case.id]
        paid = case.status == CaseStatus.RESOLVED_PAID
        filed_cb = any(d.action == DecisionAction.FILE_CHARGEBACK
                       for d in self.store.decisions_for_case(case.id))
        late = False
        if filed_cb:
            # a filing attempt landed past the network window iff the filing clock
            # was never satisfied by the chassis (it refuses late filings)
            from app.chassis import clocks as clk
            from app.domain.enums import ClockState, ClockType
            nf = clk.get_clock(case, ClockType.NETWORK_FILING_WINDOW)
            late = bool(nf and nf.satisfied_at is None)
        o = Outcome(
            case_id=case.id,
            resolution="PAID" if paid else "DENIED",
            liability="ISSUER" if paid else "CONSUMER",
            loss_amount=claim.amount if paid else 0.0,
            representment_filed=filed_cb and not late,
            recovery_forfeited_late_filing=late,
            closed_at=now,
            label_first_party=(claim.true_type == TrueType.FIRST_PARTY_MISUSE))
        self.store.put_outcome(o)
        self.ledger.append(now, case.id, "OUTCOME_RECORDED", {
            "resolution": o.resolution, "loss": o.loss_amount,
            "representment_filed": o.representment_filed,
            "late_filing_forfeit": late})
        if o.representment_filed:
            ctx.representment_due_day = self.day + ctx.rng.randint(7, 28)
        # wrongly denied non-first-party claims sometimes come back
        if (not paid and claim.true_type != TrueType.FIRST_PARTY_MISUSE
                and ctx.rng.random() < 0.35):
            self.reopen_queue.append(WorkItem(case.id, self.day +
                                              ctx.rng.randint(4, 15)))

    # ------------------------------------------------------------- plumbing
    def _execute(self, decision, case: Case, claim: Claim, account) -> None:
        # outcome recording happens at the call sites AFTER all sibling decisions
        # (pay + recovery filing) have executed, so representment_filed is accurate
        events = self.chassis.execute(decision, case, claim, account,
                                      self.clock.now())
        self._ledger_chassis(events, self.clock.now())

    def _ledger_chassis(self, events: list[ChassisEvent], now: datetime) -> None:
        for e in events:
            self.ledger.append(now, e.case_id, f"CHASSIS:{e.event_type}", e.detail)
