"""Regulatory clock engine (chassis-owned).

Encodes the Reg E / Reg Z / network deadline ladder as machine-enforced clocks.
The engine only *tracks and transitions* clocks and emits events; any resulting
customer-affecting action (e.g. provisional credit) is decided by the decisioning
layer and executed back through the chassis (PLAN.md §15.3).

Key regulatory encodings:
- Reg E investigate: 10 business days from notice (20 for new accounts). 12 CFR 1005.11(c).
- Reg E extended resolve: 45 calendar days (90 for POS / foreign / new-account txns),
  AVAILABLE ONLY IF provisional credit was posted within the investigate window;
  otherwise the investigate deadline IS the resolve deadline (critique finding #2).
- Reg E PC notice: 2 business days after posting provisional credit.
- Reg E correction/report: 1 / 3 business days after determination.
- Reg Z acknowledgment: 30 calendar days. Resolution: min(2nd billing-cycle close
  after notice, 90 calendar days). 12 CFR 1026.13.
- Network filing window: 120 calendar days from transaction date (card only).
- VCR merchant response: 30 calendar days from chargeback filing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.chassis.calendar import add_business_days, add_calendar_days, business_days_between
from app.domain.enums import ClockState, ClockType, PaymentChannel, RegRegime
from app.domain.models import Account, Case, Claim, RegClock, Transaction

AT_RISK_BUSINESS_DAYS = 2


@dataclass
class ClockEvent:
    case_id: str
    clock_type: ClockType
    transition: str            # e.g. "RUNNING->AT_RISK", "RUNNING->BREACHED"
    due_at: datetime


def _clock(ct: ClockType, start: datetime, due: datetime, basis: str) -> RegClock:
    return RegClock(clock_type=ct, started_at=start, due_at=due, basis=basis)


def _second_cycle_close(filed: datetime, cycle_close_day: int) -> datetime:
    """Date of the second billing-cycle close on/after the notice date."""
    d = filed
    closes = 0
    while closes < 2:
        d += timedelta(days=1)
        if d.day == cycle_close_day:
            closes += 1
    return add_calendar_days(d, 0)  # end of that day


def create_clocks(case: Case, claim: Claim, account: Account, txn: Transaction,
                  now: datetime) -> list[RegClock]:
    clocks: list[RegClock] = []
    if case.regime == RegRegime.REG_E:
        inv_days = 20 if case.is_new_account else 10
        clocks.append(_clock(
            ClockType.REG_E_INVESTIGATE, now, add_business_days(now, inv_days),
            f"12 CFR 1005.11(c): {inv_days} business days from notice of error"))
        resolve_days = 90 if case.extended_window else 45
        clocks.append(_clock(
            ClockType.REG_E_RESOLVE, now, add_calendar_days(now, resolve_days),
            f"12 CFR 1005.11(c): {resolve_days} calendar days — CONDITIONAL on "
            f"provisional credit posted within the investigate window"))
    else:  # REG_Z
        clocks.append(_clock(
            ClockType.REG_Z_ACK, now, add_calendar_days(now, 30),
            "12 CFR 1026.13(c)(1): acknowledge within 30 days"))
        cycle_day = account.cycle_close_day or 28
        due = min(_second_cycle_close(now, cycle_day), add_calendar_days(now, 90))
        clocks.append(_clock(
            ClockType.REG_Z_RESOLVE, now, due,
            "12 CFR 1026.13(c)(2): 2 complete billing cycles, not exceeding 90 days"))
    if claim.channel in (PaymentChannel.CARD_CREDIT, PaymentChannel.CARD_DEBIT):
        clocks.append(_clock(
            ClockType.NETWORK_FILING_WINDOW, now,
            add_calendar_days(txn.occurred_at, 120),
            "Network rule: chargeback must be filed within 120 days of transaction"))
    return clocks


def get_clock(case: Case, ct: ClockType) -> RegClock | None:
    for c in case.clocks:
        if c.clock_type == ct:
            return c
    return None


def effective_rege_resolve_deadline(case: Case) -> datetime | None:
    """The binding Reg E resolution deadline for this case *right now*.

    If PC was posted within the investigate window, the 45/90-day clock governs.
    If not (and the case is unresolved), the investigate deadline governs.
    """
    inv = get_clock(case, ClockType.REG_E_INVESTIGATE)
    res = get_clock(case, ClockType.REG_E_RESOLVE)
    if inv is None or res is None:
        return None
    pc = case.provisional_credit
    if pc.posted_at is not None and pc.posted_at <= inv.due_at:
        return res.due_at
    return inv.due_at


def on_provisional_credit_posted(case: Case, now: datetime) -> None:
    """PC satisfies the investigate clock and starts the 2bd notice clock."""
    inv = get_clock(case, ClockType.REG_E_INVESTIGATE)
    if inv and inv.state in (ClockState.RUNNING, ClockState.AT_RISK):
        inv.state = ClockState.SATISFIED
        inv.satisfied_at = now
    case.clocks.append(_clock(
        ClockType.REG_E_PC_NOTICE, now, add_business_days(now, 2),
        "12 CFR 1005.11(c)(2)(ii): notify within 2 business days of provisional credit"))


def on_determination(case: Case, now: datetime, error_found: bool) -> None:
    """Determination satisfies investigate/resolve clocks; error found starts the
    1bd correction and 3bd reporting clocks."""
    for ct in (ClockType.REG_E_INVESTIGATE, ClockType.REG_E_RESOLVE,
               ClockType.REG_Z_RESOLVE):
        c = get_clock(case, ct)
        if c and c.state in (ClockState.RUNNING, ClockState.AT_RISK):
            c.state = ClockState.SATISFIED
            c.satisfied_at = now
    if error_found and case.regime == RegRegime.REG_E:
        case.clocks.append(_clock(
            ClockType.REG_E_CORRECT, now, add_business_days(now, 1),
            "12 CFR 1005.11(c)(2)(iii): correct within 1 business day of determination"))
        case.clocks.append(_clock(
            ClockType.REG_E_REPORT, now, add_business_days(now, 3),
            "12 CFR 1005.11: report results within 3 business days of completing "
            "investigation"))


def satisfy(case: Case, ct: ClockType, now: datetime) -> None:
    c = get_clock(case, ct)
    if c and c.state in (ClockState.RUNNING, ClockState.AT_RISK):
        c.state = ClockState.SATISFIED
        c.satisfied_at = now


def start_merchant_response_clock(case: Case, now: datetime) -> None:
    case.clocks.append(_clock(
        ClockType.VCR_MERCHANT_RESPONSE, now, add_calendar_days(now, 30),
        "VCR: merchant response window, 30 calendar days"))


def refresh(case: Case, now: datetime) -> list[ClockEvent]:
    """Advance clock states for the current time; return transition events.

    The REG_E_RESOLVE clock is evaluated against the *effective* deadline so a case
    without timely PC breaches at the investigate deadline, not at day 45.
    """
    events: list[ClockEvent] = []
    for c in case.clocks:
        if c.state not in (ClockState.RUNNING, ClockState.AT_RISK):
            continue
        due = c.due_at
        if c.clock_type == ClockType.REG_E_RESOLVE:
            eff = effective_rege_resolve_deadline(case)
            if eff is not None:
                due = eff
        if now > due:
            prev = c.state
            c.state = ClockState.BREACHED
            events.append(ClockEvent(case.id, c.clock_type,
                                     f"{prev.value}->BREACHED", due))
        elif c.state == ClockState.RUNNING and \
                business_days_between(now, due) <= AT_RISK_BUSINESS_DAYS:
            c.state = ClockState.AT_RISK
            events.append(ClockEvent(case.id, c.clock_type, "RUNNING->AT_RISK", due))
    return events
