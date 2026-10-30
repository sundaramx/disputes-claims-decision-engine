"""First-party misuse feature extraction.

CE 3.0-aligned features plus issuer-only signals the networks never see (paper §4.1,
§6.2). STRICT anti-leakage rule: every feature is computed *as of claim filing time* —
no transaction, claim, or outcome dated after `claim.filed_at` may influence any
feature (critique finding #6; enforced by test_no_leakage).
"""
from __future__ import annotations

from datetime import timedelta

from app.domain.models import Account, Claim, Customer, Transaction
from app.store.db import Store

FEATURE_NAMES = [
    "ce3_prior_undisputed_txns",     # same merchant, 120-365d before disputed txn (CE 3.0)
    "recent_merchant_txns_90d",      # same merchant, 90d before filing
    "device_continuity",             # disputed txn device seen in prior txns
    "ip_continuity",                 # disputed txn ip_prefix seen in prior txns
    "delivery_address_match",        # from the disputed transaction
    "three_ds_passed",               # 3DS frictionless/challenge pass on disputed txn
    "card_not_present",
    "account_tenure_years",          # capped at 10
    "prior_claims_count",            # lifetime claims before this one (capped 10)
    "claims_last_180d",              # serial-disputer velocity (capped 6)
    "household_claims_180d",         # claims by other household members (capped 6)
    "amount_to_avg_ratio",           # claim amount / customer's avg txn amount (capped 8)
]


def extract_features(store: Store, claim: Claim, customer: Customer,
                     account: Account, txn: Transaction) -> dict[str, float]:
    filed = claim.filed_at
    all_txns = [t for t in store.transactions_for_customer(claim.customer_id)
                if t.occurred_at < filed and t.id != txn.id]
    merchant_txns = [t for t in all_txns if t.merchant_id == txn.merchant_id]

    # CE 3.0 window: undisputed transactions at the same merchant 120-365 days
    # BEFORE the disputed transaction.
    ce3_lo = txn.occurred_at - timedelta(days=365)
    ce3_hi = txn.occurred_at - timedelta(days=120)
    ce3 = [t for t in merchant_txns
           if ce3_lo <= t.occurred_at <= ce3_hi and not t.disputed]

    recent_merchant = [t for t in merchant_txns
                       if t.occurred_at >= filed - timedelta(days=90)]

    devices = {t.device_id for t in all_txns if t.device_id}
    ips = {t.ip_prefix for t in all_txns if t.ip_prefix}

    prior_claims = [c for c in store.claims_for_customer(claim.customer_id)
                    if c.filed_at < filed and c.id != claim.id]
    claims_180 = [c for c in prior_claims
                  if c.filed_at >= filed - timedelta(days=180)]

    household_claims = 0
    if customer.household_id:
        # household members' claims in the trailing 180 days (issuer-only signal)
        for c in prior_claims:
            pass  # own claims counted above
        household_claims = _household_claims(store, customer, claim)

    avg_amount = (sum(t.amount for t in all_txns) / len(all_txns)) if all_txns else 0.0
    ratio = min(claim.amount / avg_amount, 8.0) if avg_amount > 0 else 4.0

    return {
        "ce3_prior_undisputed_txns": float(min(len(ce3), 5)),
        "recent_merchant_txns_90d": float(min(len(recent_merchant), 6)),
        "device_continuity": 1.0 if (txn.device_id and txn.device_id in devices) else 0.0,
        "ip_continuity": 1.0 if (txn.ip_prefix and txn.ip_prefix in ips) else 0.0,
        "delivery_address_match": (1.0 if txn.delivery_address_match
                                   else 0.5 if txn.delivery_address_match is None else 0.0),
        "three_ds_passed": 1.0 if (txn.three_ds_result or "").endswith("PASS") else 0.0,
        "card_not_present": 0.0 if txn.card_present else 1.0,
        "account_tenure_years": min((filed - account.opened_at).days / 365.0, 10.0),
        "prior_claims_count": float(min(len(prior_claims), 10)),
        "claims_last_180d": float(min(len(claims_180), 6)),
        "household_claims_180d": float(min(household_claims, 6)),
        "amount_to_avg_ratio": ratio,
    }


def _household_claims(store: Store, customer: Customer, claim: Claim) -> int:
    """Claims filed by other members of the same household in the last 180 days,
    as of this claim's filing time. Demo-scale scan; a real system uses the graph
    feature store (paper §4.1)."""
    count = 0
    cutoff = claim.filed_at - timedelta(days=180)
    rows = store._rows(
        "SELECT c.doc FROM claims c JOIN customers cu ON cu.id = c.customer_id "
        "WHERE json_extract(cu.doc,'$.household_id') = ? AND c.customer_id != ?",
        (customer.household_id, customer.id))
    for r in rows:
        c = Claim.model_validate_json(r["doc"])
        if cutoff <= c.filed_at < claim.filed_at:
            count += 1
    return count


def as_vector(features: dict[str, float]) -> list[float]:
    return [features[n] for n in FEATURE_NAMES]
