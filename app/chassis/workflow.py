"""Case-management chassis: workflow state, SLA clocks, accounting events.

The 'buy/retain' layer (paper §5, §7). It executes decisions handed to it and
reports what it did as ChassisEvents; it never decides anything judgmental and
never imports the decisioning layer. The orchestrator (app/engine.py) ledgers the
events this layer emits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.chassis import clocks as clk
from app.domain.enums import (CaseStatus, ClockType, DecisionAction, PaymentChannel,
                              RegRegime)
from app.domain.models import Account, Case, Claim, Decision, Transaction
from app.store.db import Store

PC_INTEREST_APR = 0.02      # simple interest on provisional credit (examiner finding)

ROUTING: dict[PaymentChannel, RegRegime] = {
    PaymentChannel.CARD_DEBIT: RegRegime.REG_E,
    PaymentChannel.ZELLE: RegRegime.REG_E,
    PaymentChannel.ACH: RegRegime.REG_E,
    PaymentChannel.CARD_CREDIT: RegRegime.REG_Z,
}


@dataclass
class ChassisEvent:
    case_id: str
    event_type: str
    detail: dict


class CaseChassis:
    def __init__(self, store: Store):
        self.store = store

    # ---------------- case opening ----------------
    def open_case(self, claim: Claim, account: Account, txn: Transaction,
                  now: datetime) -> tuple[Case, list[ChassisEvent]]:
        regime = ROUTING[claim.channel]
        is_new = account.is_new_account(claim.filed_at)
        extended = (regime == RegRegime.REG_E and
                    (is_new or txn.foreign or txn.card_present))
        case = Case(id=f"CS{claim.id[2:]}", claim_id=claim.id,
                    customer_id=claim.customer_id, account_id=claim.account_id,
                    regime=regime, is_new_account=is_new, extended_window=extended,
                    status=CaseStatus.TRIAGE, assigned_queue="TRIAGE", opened_at=now)
        case.clocks = clk.create_clocks(case, claim, account, txn, now)
        self.store.put_case(case)
        events = [ChassisEvent(case.id, "CASE_OPENED", {
            "claim_id": claim.id, "regime": regime.value,
            "regime_basis": ("12 CFR 1005 (EFT: debit/Zelle/ACH)"
                             if regime == RegRegime.REG_E
                             else "12 CFR 1026.13 (credit billing error)"),
            "is_new_account": is_new, "extended_window": extended,
            "clocks": [{"type": c.clock_type.value, "due": c.due_at.isoformat(),
                        "basis": c.basis} for c in case.clocks]})]
        return case, events

    # ---------------- decision execution ----------------
    def execute(self, decision: Decision, case: Case, claim: Claim,
                account: Account, now: datetime) -> list[ChassisEvent]:
        a = decision.action
        ev: list[ChassisEvent] = []

        if a == DecisionAction.ACTIVATE_REG_Z_PENDENCY:
            p = case.reg_z_pendency
            p.activated_at, p.payment_hold = now, True
            p.finance_charge_freeze, p.adverse_reporting_block = True, True
            p.ack_sent_at = now
            clk.satisfy(case, ClockType.REG_Z_ACK, now)
            ev.append(ChassisEvent(case.id, "REG_Z_PENDENCY_ACTIVATED", {
                "payment_hold": True, "finance_charge_freeze": True,
                "adverse_reporting_block": True, "ack_letter": "sent"}))

        elif a == DecisionAction.POST_PROVISIONAL_CREDIT:
            if case.regime != RegRegime.REG_E:
                raise ValueError("Provisional credit is a Reg E instrument only")
            pc = case.provisional_credit
            if pc.posted_at is None:
                pc.posted_at, pc.amount = now, claim.amount
                pc.notice_sent_at = now          # notice issued with posting (2bd rule)
                clk.on_provisional_credit_posted(case, now)
                clk.satisfy(case, ClockType.REG_E_PC_NOTICE, now)
                ev.append(ChassisEvent(case.id, "ACCT_PROVISIONAL_CREDIT_POSTED", {
                    "amount": claim.amount, "interest_bearing": account.interest_bearing,
                    "notice": "issued with posting (12 CFR 1005.11(c)(2)(ii))"}))

        elif a in (DecisionAction.AUTO_PAY, DecisionAction.APPROVE_PAY):
            self._determine(case, now, error_found=True)
            case.status = CaseStatus.RESOLVED_PAID
            case.resolved_at = now
            case.assigned_queue = "CLOSED"
            self._finalize_pc_interest(case, account, now)
            ev.append(ChassisEvent(case.id, "ACCT_CONSUMER_PAID", {
                "amount": claim.amount, "final": True,
                "pc_interest": case.provisional_credit.interest_accrued}))

        elif a == DecisionAction.APPROVE_DENIAL:
            self._determine(case, now, error_found=False)
            case.status = CaseStatus.RESOLVED_DENIED
            case.resolved_at = now
            case.assigned_queue = "CLOSED"
            pc = case.provisional_credit
            if pc.posted_at and not pc.reversed_at:
                pc.reversal_notice_at = now
                pc.reversed_at = clk.add_business_days(now, 5)  # notice + grace period
                ev.append(ChassisEvent(case.id, "ACCT_PC_REVERSAL_SCHEDULED", {
                    "amount": pc.amount,
                    "grace": "5 business days after notice; debits honored"}))
            ev.append(ChassisEvent(case.id, "DENIAL_LETTER_SENT", {
                "template": "compliance-approved denial with evidence summary"}))

        elif a == DecisionAction.FILE_CHARGEBACK:
            nf = clk.get_clock(case, ClockType.NETWORK_FILING_WINDOW)
            late = bool(nf and now > nf.due_at)
            if not late:
                clk.satisfy(case, ClockType.NETWORK_FILING_WINDOW, now)
                clk.start_merchant_response_clock(case, now)
            if case.status not in (CaseStatus.RESOLVED_PAID, CaseStatus.RESOLVED_DENIED):
                case.status = CaseStatus.REPRESENTMENT
            ev.append(ChassisEvent(case.id, "CHARGEBACK_FILED", {
                "late": late, "network_window_due":
                nf.due_at.isoformat() if nf else None,
                "note": "recovery forfeited — filed past network window" if late
                        else "filed within window; merchant has 30 days"}))

        elif a == DecisionAction.ROUTE_STANDARD_INVESTIGATION:
            case.status = CaseStatus.INVESTIGATION
            case.assigned_queue = "INVESTIGATION_STANDARD"
            ev.append(ChassisEvent(case.id, "QUEUED", {"queue": case.assigned_queue}))

        elif a == DecisionAction.ROUTE_ENHANCED_INVESTIGATION:
            case.status = CaseStatus.INVESTIGATION
            case.assigned_queue = "INVESTIGATION_ENHANCED"
            ev.append(ChassisEvent(case.id, "QUEUED", {"queue": case.assigned_queue}))

        elif a == DecisionAction.ROUTE_HUMAN_REVIEW:
            case.status = CaseStatus.PENDING_REVIEW
            case.assigned_queue = "HUMAN_REVIEW"
            case.touches += 1
            ev.append(ChassisEvent(case.id, "QUEUED", {"queue": case.assigned_queue}))

        self.store.put_case(case)
        return ev

    # ---------------- clock maintenance ----------------
    def refresh_clocks(self, case: Case, now: datetime) -> list[clk.ClockEvent]:
        events = clk.refresh(case, now)
        for e in events:
            if "BREACHED" in e.transition and not case.escalated:
                case.escalated = True
                case.assigned_queue = "ESCALATION"
        if events:
            self.store.put_case(case)
        return events

    # ---------------- internals ----------------
    @staticmethod
    def _determine(case: Case, now: datetime, error_found: bool) -> None:
        if case.determined_at is None:
            case.determined_at = now
            clk.on_determination(case, now, error_found)
            if error_found:
                # correction is executed by the same accounting action; report
                # follows immediately in this build
                clk.satisfy(case, ClockType.REG_E_CORRECT, now)
                clk.satisfy(case, ClockType.REG_E_REPORT, now)

    @staticmethod
    def _finalize_pc_interest(case: Case, account: Account, now: datetime) -> None:
        pc = case.provisional_credit
        if pc.posted_at and account.interest_bearing:
            days = max((now - pc.posted_at).days, 0)
            pc.interest_accrued = round(pc.amount * PC_INTEREST_APR * days / 365, 2)
