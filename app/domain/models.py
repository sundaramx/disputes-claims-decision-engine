"""Domain entities. Pydantic models are used end-to-end (store <-> engine <-> API)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .enums import (
    Actor,
    CaseStatus,
    ClaimReason,
    ClockState,
    ClockType,
    Cohort,
    DecisionAction,
    NetworkCategory,
    PaymentChannel,
    RegRegime,
    Tier,
    TrueType,
)


class Customer(BaseModel):
    id: str
    name: str
    segment: str = "MASS"                      # MASS | AFFLUENT | PRIVATE
    tenure_days: int = 0
    household_id: Optional[str] = None
    opened_at: datetime


class Account(BaseModel):
    id: str
    customer_id: str
    channel_type: PaymentChannel               # primary rail for this account
    opened_at: datetime
    interest_bearing: bool = False
    # Credit accounts only: day of month the billing cycle closes (Reg Z deadline math).
    cycle_close_day: Optional[int] = None

    def is_new_account(self, as_of: datetime) -> bool:
        """Reg E new-account: notice within 30 days of first deposit; we approximate
        with account age <= 30 calendar days at claim filing."""
        return (as_of - self.opened_at).days <= 30


class Transaction(BaseModel):
    id: str
    account_id: str
    customer_id: str
    merchant_id: str
    merchant_name: str
    mcc: str
    amount: float
    channel: PaymentChannel
    card_present: bool = False
    foreign: bool = False
    device_id: Optional[str] = None
    ip_prefix: Optional[str] = None
    three_ds_result: Optional[str] = None      # e.g. "FRICTIONLESS_PASS", "CHALLENGE_PASS"
    auth_result: str = "APPROVED"
    delivery_address_match: Optional[bool] = None
    occurred_at: datetime
    disputed: bool = False


class Claim(BaseModel):
    id: str
    account_id: str
    customer_id: str
    transaction_id: str
    reason: ClaimReason
    channel: PaymentChannel
    amount: float
    narrative: str = ""
    intake_channel: str = "APP"                # APP | CALL | BRANCH
    filed_at: datetime
    # Card claims only; None for Zelle/ACH (no chargeback rights).
    network_category: Optional[NetworkCategory] = None
    # Simulation-only ground truth; excluded from all engine feature extraction.
    true_type: Optional[TrueType] = None


class RegClock(BaseModel):
    clock_type: ClockType
    started_at: datetime
    due_at: datetime
    state: ClockState = ClockState.RUNNING
    satisfied_at: Optional[datetime] = None
    basis: str = ""                            # human-readable rule citation


class ProvisionalCredit(BaseModel):
    """Reg E only. Reg Z cases use RegZPendency instead."""
    required_by: Optional[datetime] = None
    posted_at: Optional[datetime] = None
    amount: float = 0.0
    interest_accrued: float = 0.0              # interest-bearing accounts (examiner finding)
    notice_sent_at: Optional[datetime] = None
    reversed_at: Optional[datetime] = None
    reversal_notice_at: Optional[datetime] = None


class RegZPendency(BaseModel):
    """Reg Z pendency protections: disputed amount not payable, finance-charge freeze,
    adverse-credit-reporting block. The Reg Z analogue of provisional credit."""
    activated_at: Optional[datetime] = None
    payment_hold: bool = False
    finance_charge_freeze: bool = False
    adverse_reporting_block: bool = False
    ack_sent_at: Optional[datetime] = None     # 30-day acknowledgment letter
    released_at: Optional[datetime] = None


class Case(BaseModel):
    id: str
    claim_id: str
    customer_id: str
    account_id: str
    status: CaseStatus = CaseStatus.INTAKE
    regime: RegRegime
    is_new_account: bool = False
    extended_window: bool = False              # Reg E 90-day (POS/foreign/new-account)
    tier: Optional[Tier] = None
    cohort: Optional[Cohort] = None
    policy_pack_id: Optional[str] = None
    misuse_score: Optional[float] = None
    score_reasons: list[dict] = Field(default_factory=list)
    clocks: list[RegClock] = Field(default_factory=list)
    provisional_credit: ProvisionalCredit = Field(default_factory=ProvisionalCredit)
    reg_z_pendency: RegZPendency = Field(default_factory=RegZPendency)
    assigned_queue: str = "TRIAGE"
    touches: int = 0                           # human touch count (unit-cost proxy)
    opened_at: datetime
    determined_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    escalated: bool = False


class Decision(BaseModel):
    id: str
    case_id: str
    stage: str                                 # TRIAGE | ADJUDICATION | INVESTIGATION | ...
    action: DecisionAction
    actor: Actor
    policy_pack_id: Optional[str] = None
    policy_version: Optional[str] = None
    rule_id: Optional[str] = None
    model_version: Optional[str] = None
    score: Optional[float] = None
    reasons: list[dict] = Field(default_factory=list)
    rationale: str = ""
    cohort: Optional[Cohort] = None
    decided_at: datetime
    ledger_seq: Optional[int] = None


class EvidenceItem(BaseModel):
    id: str
    case_id: str
    source: str                                # AUTH_LOG | THREE_DS | ORDER_INSIGHT | ...
    summary: str
    payload: dict = Field(default_factory=dict)
    gathered_at: datetime
    gathered_by: str = "EVIDENCE_AGENT"


class Outcome(BaseModel):
    case_id: str
    resolution: str                            # PAID | DENIED | RECOVERED | DEFLECTED
    liability: str                             # ISSUER | MERCHANT | CONSUMER
    loss_amount: float = 0.0
    recovered_amount: float = 0.0
    representment_filed: bool = False
    representment_won: Optional[bool] = None
    recovery_forfeited_late_filing: bool = False   # filed past network window (KPI)
    reopened: bool = False
    closed_at: datetime
    label_first_party: Optional[bool] = None   # learning-loop label once known


class Inquiry(BaseModel):
    """Pre-claim customer inquiry — the deflection surface (Order Insight lookup at the
    point of contact, before a claim exists)."""
    id: str
    customer_id: str
    transaction_id: str
    occurred_at: datetime
    order_insight_hit: bool = False
    deflected: bool = False                    # recognized txn -> no claim filed
    converted_claim_id: Optional[str] = None


class LedgerEntry(BaseModel):
    seq: int
    ts: datetime
    case_id: str
    event_type: str
    payload: dict
    payload_hash: str
    prev_hash: str
    hash: str
