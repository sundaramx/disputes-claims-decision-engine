"""Synthetic portfolio and claim-flow generator.

Honesty-by-construction (critique findings #5, #6):
- Class-conditional feature distributions OVERLAP and every claim's observable
  characteristics are perturbed with noise, so the trained model is good but not
  perfect (realistic AUC, honest calibration).
- Ground-truth labels (`true_type`) are never readable by the engine at decision
  time; they drive only evidence reveals and outcome sampling in the lifecycle driver.
- A scheduled mid-run mix shift raises the first-party-misuse share so the drift
  monitor (PSI) demonstrably fires.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.domain.enums import ClaimReason, NetworkCategory, PaymentChannel, TrueType
from app.domain.models import Account, Claim, Customer, Transaction
from app.store.db import Store

UTC = timezone.utc

CHANNEL_MIX = [(PaymentChannel.CARD_CREDIT, 0.45), (PaymentChannel.CARD_DEBIT, 0.35),
               (PaymentChannel.ZELLE, 0.12), (PaymentChannel.ACH, 0.08)]

# Claim-mix by simulation day: (first_party, third_party, merchant_error, legit_other)
BASE_MIX = {TrueType.FIRST_PARTY_MISUSE: 0.28, TrueType.THIRD_PARTY_FRAUD: 0.34,
            TrueType.MERCHANT_ERROR: 0.22, TrueType.LEGITIMATE_OTHER: 0.16}
SHIFTED_MIX = {TrueType.FIRST_PARTY_MISUSE: 0.54, TrueType.THIRD_PARTY_FRAUD: 0.20,
               TrueType.MERCHANT_ERROR: 0.14, TrueType.LEGITIMATE_OTHER: 0.12}
MIX_SHIFT_DAY = 45          # drift injection point

NOISE = 0.18                # probability of flipping a class-typical attribute


@dataclass
class Merchant:
    id: str
    name: str
    mcc: str
    order_insight: bool     # participates in Verifi Order Insight / Consumer Clarity


@dataclass
class ClaimPlan:
    """A claim (or pre-claim inquiry) scheduled for a simulation day."""
    day: int
    claim: Claim
    starts_as_inquiry: bool
    evidence_quality: float          # 0..1, how conclusive gathered evidence will be


@dataclass
class Portfolio:
    merchants: list[Merchant] = field(default_factory=list)
    customers: list[Customer] = field(default_factory=list)
    accounts: dict[str, Account] = field(default_factory=dict)   # customer_id -> account
    plans: list[ClaimPlan] = field(default_factory=list)


def _pick(rng: random.Random, weighted: dict | list) -> object:
    items = list(weighted.items()) if isinstance(weighted, dict) else weighted
    r, acc = rng.random(), 0.0
    for k, w in items:
        acc += w
        if r <= acc:
            return k
    return items[-1][0]


def generate_portfolio(store: Store, rng: random.Random, sim_start: datetime,
                       n_customers: int = 600, n_claims: int = 900,
                       horizon_days: int = 80) -> Portfolio:
    p = Portfolio()
    p.merchants = [
        Merchant(id=f"M{i:03d}", name=name, mcc=mcc, order_insight=rng.random() < 0.6)
        for i, (name, mcc) in enumerate(
            [(f"{brand}{i}", rng.choice(["5411", "5732", "5812", "5999", "4899", "5967"]))
             for i, brand in enumerate(
                 ["ShopMart", "StreamCo", "FlyAir", "GameHub", "DineOut", "TechStore",
                  "FitClub", "CloudSoft", "TravelNow", "MegaGrocer"] * 6)])]

    # customers, accounts, baseline transaction history
    for i in range(n_customers):
        household = f"H{i // 2:04d}" if rng.random() < 0.25 else f"H{i:04d}s"
        opened = sim_start - timedelta(days=rng.randint(45, 3000))
        cust = Customer(id=f"CU{i:05d}", name=f"Customer {i}",
                        segment=rng.choice(["MASS", "MASS", "MASS", "AFFLUENT"]),
                        tenure_days=(sim_start - opened).days, household_id=household,
                        opened_at=opened)
        channel = _pick(rng, CHANNEL_MIX)
        # ~7% genuinely new accounts (Reg E 20bd/90d path needs population)
        acct_opened = (sim_start - timedelta(days=rng.randint(3, 25))
                       if rng.random() < 0.07 else opened)
        acct = Account(id=f"AC{i:05d}", customer_id=cust.id, channel_type=channel,
                       opened_at=acct_opened,
                       interest_bearing=(channel == PaymentChannel.CARD_DEBIT
                                         and rng.random() < 0.5),
                       cycle_close_day=(rng.randint(1, 28)
                                        if channel == PaymentChannel.CARD_CREDIT else None))
        store.put_customer(cust)
        store.put_account(acct)
        p.customers.append(cust)
        p.accounts[cust.id] = acct
        _baseline_history(store, rng, cust, acct, p.merchants, sim_start)

    # claim plans across the horizon, with the scheduled mix shift
    filing_days = sorted(rng.randint(1, horizon_days - 5) for _ in range(n_claims))
    for j, day in enumerate(filing_days):
        mix = SHIFTED_MIX if day >= MIX_SHIFT_DAY else BASE_MIX
        true_type = _pick(rng, mix)
        cust = rng.choice(p.customers)
        acct = p.accounts[cust.id]
        plan = _plan_claim(store, rng, j, cust, acct, p.merchants, sim_start, day,
                           true_type)
        p.plans.append(plan)
    return p


def plan_claims_for_day(store: Store, rng: random.Random, p: Portfolio,
                        sim_start: datetime, day: int, n: int) -> list[ClaimPlan]:
    """Open-ended arrivals for continuous (live) simulation: n new claim plans filed
    on `day`, numbered after every plan generated so far."""
    mix = SHIFTED_MIX if day >= MIX_SHIFT_DAY else BASE_MIX
    plans = []
    for _ in range(n):
        cust = rng.choice(p.customers)
        plan = _plan_claim(store, rng, len(p.plans), cust, p.accounts[cust.id],
                           p.merchants, sim_start, day, _pick(rng, mix))
        p.plans.append(plan)
        plans.append(plan)
    return plans


def _baseline_history(store: Store, rng: random.Random, cust: Customer, acct: Account,
                      merchants: list[Merchant], sim_start: datetime) -> None:
    """20-70 undisputed transactions over the ~400 days before simulation start,
    concentrated at 3-8 'home' merchants, with 1-3 stable devices/IPs."""
    n_txn = rng.randint(20, 70)
    home = rng.sample(merchants, k=rng.randint(3, 8))
    devices = [f"D{cust.id}{k}" for k in range(rng.randint(1, 3))]
    ips = [f"IP{cust.id}{k}" for k in range(rng.randint(1, 2))]
    for t in range(n_txn):
        m = rng.choice(home) if rng.random() < 0.75 else rng.choice(merchants)
        occurred = sim_start - timedelta(days=rng.randint(1, 400),
                                         hours=rng.randint(0, 23))
        store.put_transaction(Transaction(
            id=f"T{cust.id}-{t:03d}", account_id=acct.id, customer_id=cust.id,
            merchant_id=m.id, merchant_name=m.name, mcc=m.mcc,
            amount=round(rng.uniform(8, 400), 2), channel=acct.channel_type,
            card_present=rng.random() < 0.35, foreign=rng.random() < 0.05,
            device_id=rng.choice(devices), ip_prefix=rng.choice(ips),
            three_ds_result=rng.choice(["FRICTIONLESS_PASS", "CHALLENGE_PASS", None]),
            delivery_address_match=rng.random() < 0.9, occurred_at=occurred))


def _plan_claim(store: Store, rng: random.Random, j: int, cust: Customer,
                acct: Account, merchants: list[Merchant], sim_start: datetime,
                day: int, true_type: TrueType) -> ClaimPlan:
    filed_at = sim_start + timedelta(days=day, hours=rng.randint(8, 18))
    noisy = lambda base: (not base) if rng.random() < NOISE else base  # noqa: E731

    prior = store.transactions_for_customer(cust.id)
    devices = [t.device_id for t in prior if t.device_id] or [f"D{cust.id}0"]
    ips = [t.ip_prefix for t in prior if t.ip_prefix] or [f"IP{cust.id}0"]

    first_party = true_type == TrueType.FIRST_PARTY_MISUSE
    merchant_err = true_type == TrueType.MERCHANT_ERROR

    # merchant: first-party & merchant-error claims mostly hit familiar merchants
    if (first_party or merchant_err) and prior and not (rng.random() < NOISE):
        mid = rng.choice(prior).merchant_id
        merchant = next(m for m in merchants if m.id == mid)
    else:
        merchant = rng.choice(merchants)

    # a few claims are filed >120d after the txn to exercise the late-filing clock
    txn_age = rng.randint(100, 140) if rng.random() < 0.05 else rng.randint(2, 45)
    occurred = filed_at - timedelta(days=txn_age)

    known_device = noisy(first_party or merchant_err)
    known_ip = noisy(first_party or merchant_err)
    # log-normal claim amounts: median ~$80, long right tail (real claim mixes skew
    # low-value — that's what makes T0 straight-through meaningful, paper §4.2)
    amount = round(min(rng.lognormvariate(4.4, 0.95), 2500.0)
                   * (1.6 if first_party else 1.0), 2)

    txn = Transaction(
        id=f"TX-CLAIM-{j:05d}", account_id=acct.id, customer_id=cust.id,
        merchant_id=merchant.id, merchant_name=merchant.name, mcc=merchant.mcc,
        amount=amount, channel=acct.channel_type,
        card_present=noisy(False) if not first_party else noisy(rng.random() < 0.3),
        foreign=rng.random() < (0.12 if true_type == TrueType.THIRD_PARTY_FRAUD else 0.04),
        device_id=rng.choice(devices) if known_device else f"DNEW{j:05d}",
        ip_prefix=rng.choice(ips) if known_ip else f"IPNEW{j:05d}",
        three_ds_result=("FRICTIONLESS_PASS" if noisy(first_party) else
                         rng.choice([None, "ATTEMPTED", "FAILED"])),
        delivery_address_match=noisy(first_party or merchant_err),
        occurred_at=occurred, disputed=True)
    store.put_transaction(txn)

    # CE 3.0 ammunition: first-party claimants usually have 120-365d history there
    if first_party and rng.random() > NOISE:
        for k in range(rng.randint(2, 4)):
            back = rng.randint(130, 350)
            store.put_transaction(Transaction(
                id=f"TXH-{j:05d}-{k}", account_id=acct.id, customer_id=cust.id,
                merchant_id=merchant.id, merchant_name=merchant.name, mcc=merchant.mcc,
                amount=round(rng.uniform(10, 300), 2), channel=acct.channel_type,
                card_present=False, device_id=rng.choice(devices),
                ip_prefix=rng.choice(ips), three_ds_result="FRICTIONLESS_PASS",
                delivery_address_match=True,
                occurred_at=occurred - timedelta(days=back)))

    reason, category = _reason_and_category(rng, true_type, acct.channel_type, txn)
    claim = Claim(
        id=f"CL{j:05d}", account_id=acct.id, customer_id=cust.id,
        transaction_id=txn.id, reason=reason, channel=acct.channel_type,
        amount=amount, narrative=f"Customer states: {reason.value.lower()}",
        intake_channel=rng.choice(["APP", "APP", "CALL", "BRANCH"]),
        filed_at=filed_at, network_category=category, true_type=true_type)

    evidence_quality = rng.uniform(0.55, 0.95) if first_party else rng.uniform(0.4, 0.9)
    return ClaimPlan(day=day, claim=claim,
                     starts_as_inquiry=rng.random() < 0.5,
                     evidence_quality=evidence_quality)


def _reason_and_category(rng: random.Random, true_type: TrueType,
                         channel: PaymentChannel, txn: Transaction):
    is_card = channel in (PaymentChannel.CARD_CREDIT, PaymentChannel.CARD_DEBIT)
    if true_type in (TrueType.FIRST_PARTY_MISUSE, TrueType.THIRD_PARTY_FRAUD):
        reason = ClaimReason.FRAUD_UNAUTHORIZED
        cat = (None if not is_card else
               (NetworkCategory.FRAUD_CP if txn.card_present else NetworkCategory.FRAUD_CNP))
        return reason, cat
    if true_type == TrueType.MERCHANT_ERROR:
        reason = rng.choice([ClaimReason.DUPLICATE_CHARGE, ClaimReason.INCORRECT_AMOUNT,
                             ClaimReason.CREDIT_NOT_PROCESSED])
        return reason, (NetworkCategory.PROCESSING_ERROR if is_card else None)
    reason = rng.choice([ClaimReason.GOODS_NOT_RECEIVED, ClaimReason.NOT_AS_DESCRIBED,
                         ClaimReason.CANCELLED_RECURRING])
    return reason, (NetworkCategory.CONSUMER_DISPUTE if is_card else None)
