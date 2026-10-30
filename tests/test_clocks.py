"""Clock engine tests: the compliance-critical core."""
from datetime import date, datetime, timezone

import pytest

from app.chassis.calendar import add_business_days, add_calendar_days, is_business_day
from app.chassis import clocks
from app.domain.enums import (CaseStatus, ClaimReason, ClockState, ClockType,
                              PaymentChannel, RegRegime)
from app.domain.models import Account, Case, Claim, Transaction

UTC = timezone.utc


def dt(y, m, d, h=9):
    return datetime(y, m, d, h, tzinfo=UTC)


def make_case(regime=RegRegime.REG_E, new_account=False, extended=False,
              channel=PaymentChannel.CARD_DEBIT, filed=None):
    filed = filed or dt(2026, 1, 5)  # a Monday
    claim = Claim(id="cl1", account_id="a1", customer_id="cu1", transaction_id="t1",
                  reason=ClaimReason.FRAUD_UNAUTHORIZED, channel=channel, amount=100.0,
                  filed_at=filed)
    acct = Account(id="a1", customer_id="cu1", channel_type=channel,
                   opened_at=dt(2024, 1, 1), cycle_close_day=15)
    txn = Transaction(id="t1", account_id="a1", customer_id="cu1", merchant_id="m1",
                      merchant_name="M", mcc="5999", amount=100.0, channel=channel,
                      occurred_at=dt(2025, 12, 20))
    case = Case(id="c1", claim_id="cl1", customer_id="cu1", account_id="a1",
                regime=regime, is_new_account=new_account, extended_window=extended,
                opened_at=filed, status=CaseStatus.TRIAGE)
    case.clocks = clocks.create_clocks(case, claim, acct, txn, filed)
    return case


# ---------- business-day calendar ----------

def test_weekend_and_holiday_not_business_days():
    assert not is_business_day(date(2026, 1, 3))    # Saturday
    assert not is_business_day(date(2026, 1, 1))    # New Year's Day
    assert not is_business_day(date(2026, 7, 3))    # July 4 observed
    assert is_business_day(date(2026, 1, 5))        # Monday


def test_add_business_days_skips_weekends_and_holidays():
    # Mon 2026-01-12 + 5 bd -> Mon 2026-01-19 is MLK (holiday) -> Tue 2026-01-20
    out = add_business_days(dt(2026, 1, 12), 5)
    assert out.date() == date(2026, 1, 20)


def test_ten_business_days_from_notice():
    # Filed Mon 2026-01-05; 10 bd later: Jan 6,7,8,9,12,13,14,15,16,20 (19th is MLK)
    out = add_business_days(dt(2026, 1, 5), 10)
    assert out.date() == date(2026, 1, 20)


# ---------- Reg E clock creation ----------

def test_rege_clocks_created_10bd_45cd():
    case = make_case()
    inv = clocks.get_clock(case, ClockType.REG_E_INVESTIGATE)
    res = clocks.get_clock(case, ClockType.REG_E_RESOLVE)
    assert inv.due_at.date() == date(2026, 1, 20)   # 10 bd
    assert res.due_at.date() == date(2026, 2, 19)   # 45 cd
    # card claim also carries the network filing window from the TRANSACTION date
    nf = clocks.get_clock(case, ClockType.NETWORK_FILING_WINDOW)
    assert nf.due_at.date() == date(2026, 4, 19)    # txn 2025-12-20 + 120


def test_new_account_gets_20bd_and_90cd():
    case = make_case(new_account=True, extended=True)
    inv = clocks.get_clock(case, ClockType.REG_E_INVESTIGATE)
    res = clocks.get_clock(case, ClockType.REG_E_RESOLVE)
    assert inv.due_at.date() == date(2026, 2, 3)    # 20 bd (skips MLK)
    assert res.due_at.date() == date(2026, 4, 5)    # 90 cd


def test_zelle_has_no_network_filing_clock():
    case = make_case(channel=PaymentChannel.ZELLE)
    assert clocks.get_clock(case, ClockType.NETWORK_FILING_WINDOW) is None


# ---------- conditional 45/90 extension (critique finding #2) ----------

def test_no_pc_means_investigate_deadline_is_resolve_deadline():
    case = make_case()
    eff = clocks.effective_rege_resolve_deadline(case)
    assert eff.date() == date(2026, 1, 20)          # NOT Feb 19

def test_timely_pc_unlocks_45_day_window():
    case = make_case()
    case.provisional_credit.posted_at = dt(2026, 1, 15)
    clocks.on_provisional_credit_posted(case, dt(2026, 1, 15))
    eff = clocks.effective_rege_resolve_deadline(case)
    assert eff.date() == date(2026, 2, 19)

def test_resolve_breaches_at_investigate_deadline_without_pc():
    case = make_case()
    events = clocks.refresh(case, dt(2026, 1, 21))
    breached = {e.clock_type for e in events if "BREACHED" in e.transition}
    assert ClockType.REG_E_INVESTIGATE in breached
    assert ClockType.REG_E_RESOLVE in breached      # effective deadline passed


# ---------- PC side effects ----------

def test_pc_satisfies_investigate_and_starts_notice_clock():
    case = make_case()
    now = dt(2026, 1, 14)
    case.provisional_credit.posted_at = now
    clocks.on_provisional_credit_posted(case, now)
    inv = clocks.get_clock(case, ClockType.REG_E_INVESTIGATE)
    assert inv.state == ClockState.SATISFIED
    notice = clocks.get_clock(case, ClockType.REG_E_PC_NOTICE)
    assert notice.due_at.date() == date(2026, 1, 16)  # 2 bd


# ---------- Reg Z ----------

def test_regz_clocks_ack30_and_two_cycle_close_capped_90():
    case = make_case(regime=RegRegime.REG_Z, channel=PaymentChannel.CARD_CREDIT)
    ack = clocks.get_clock(case, ClockType.REG_Z_ACK)
    res = clocks.get_clock(case, ClockType.REG_Z_RESOLVE)
    assert ack.due_at.date() == date(2026, 2, 4)    # 30 cd from Jan 5
    # cycle closes on the 15th: Jan 15 + Feb 15 -> 2nd close Feb 15 < Apr 5 (90d cap)
    assert res.due_at.date() == date(2026, 2, 15)
    assert clocks.get_clock(case, ClockType.REG_E_INVESTIGATE) is None


def test_determination_starts_correct_and_report_clocks():
    case = make_case()
    clocks.on_determination(case, dt(2026, 1, 12), error_found=True)
    corr = clocks.get_clock(case, ClockType.REG_E_CORRECT)
    rep = clocks.get_clock(case, ClockType.REG_E_REPORT)
    assert corr.due_at.date() == date(2026, 1, 13)
    assert rep.due_at.date() == date(2026, 1, 15)


# ---------- at-risk / breach transitions ----------

def test_at_risk_then_breach_transitions():
    case = make_case()
    ev1 = clocks.refresh(case, dt(2026, 1, 16))     # <=2 bd remaining (Fri before due Tue)
    assert any(e.transition == "RUNNING->AT_RISK" and
               e.clock_type == ClockType.REG_E_INVESTIGATE for e in ev1)
    ev2 = clocks.refresh(case, dt(2026, 1, 21))
    assert any("BREACHED" in e.transition and
               e.clock_type == ClockType.REG_E_INVESTIGATE for e in ev2)
    # no duplicate events on re-refresh
    assert clocks.refresh(case, dt(2026, 1, 22)) == []
