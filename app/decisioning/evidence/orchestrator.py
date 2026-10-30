"""Agentic evidence orchestrator with human checkpoints (paper §4.3).

A deterministic plan->act->cite loop over typed tools. Each tool call yields an
EvidenceItem; the drafted investigator narrative cites artifacts as [E1], [E2]...
The recommendation is derived ONLY from artifact fields (never from simulation
ground truth) and is a DRAFT: any denial or network filing that follows requires a
human checkpoint (INV-NO-AUTO-DENIAL).

LLM swap-in point: `_draft_narrative` is where a generative model would draft prose
from the same structured evidence bundle; the tool loop, citations, checkpoint
gates, and ledger writes do not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.domain.enums import NetworkCategory, PaymentChannel, TrueType
from app.domain.models import Case, Claim, EvidenceItem, Transaction
from app.network.adapters import MockNetworkBackend
from app.store.db import Store

CARD = (PaymentChannel.CARD_CREDIT, PaymentChannel.CARD_DEBIT)


@dataclass
class EvidenceBundle:
    items: list[EvidenceItem] = field(default_factory=list)
    ce3_qualified: bool = False
    ce3_basis: str = ""
    recommendation: str = "INCONCLUSIVE"   # CONFIRM_FIRST_PARTY | SUPPORT_CONSUMER | INCONCLUSIVE
    confidence: float = 0.0
    narrative: str = ""


class EvidenceOrchestrator:
    def __init__(self, store: Store, network: MockNetworkBackend):
        self.store = store
        self.network = network

    def investigate(self, now, case: Case, claim: Claim, txn: Transaction,
                    evidence_quality: float) -> EvidenceBundle:
        b = EvidenceBundle()
        add = lambda src, summary, payload: b.items.append(  # noqa: E731
            EvidenceItem(id=f"E-{case.id}-{len(b.items) + 1:02d}", case_id=case.id,
                         source=src, summary=summary, payload=payload,
                         gathered_at=now))

        # -- tool 1: auth log --------------------------------------------------
        prior = [t for t in self.store.transactions_for_customer(claim.customer_id)
                 if t.occurred_at < claim.filed_at and t.id != txn.id]
        known_devices = {t.device_id for t in prior if t.device_id}
        known_ips = {t.ip_prefix for t in prior if t.ip_prefix}
        device_known = bool(txn.device_id and txn.device_id in known_devices)
        ip_known = bool(txn.ip_prefix and txn.ip_prefix in known_ips)
        add("AUTH_LOG",
            f"Auth {txn.auth_result}; device "
            f"{'KNOWN' if device_known else 'NEW'}, IP "
            f"{'KNOWN' if ip_known else 'NEW'}",
            {"device_known": device_known, "ip_known": ip_known,
             "auth_result": txn.auth_result, "card_present": txn.card_present})

        # -- tool 2: 3DS -------------------------------------------------------
        tds = txn.three_ds_result or "NOT_ENROLLED"
        add("THREE_DS", f"3DS result: {tds}", {"result": tds,
            "passed": tds.endswith("PASS")})

        # -- tool 3: Order Insight / Consumer Clarity (card only) ---------------
        oi = None
        if claim.channel in CARD:
            oi = self.network.order_insight_lookup(claim, txn, evidence_quality)
            add("ORDER_INSIGHT" if claim.channel == PaymentChannel.CARD_DEBIT
                else "CONSUMER_CLARITY",
                ("Merchant not enrolled" if not oi.participating else
                 f"Order {'found' if oi.order_found else 'not found'}; "
                 f"device_match={oi.device_match}, addr_match={oi.address_match}, "
                 f"prior_undisputed_orders={oi.prior_undisputed_orders}"),
                vars(oi))

        # -- tool 4: prior claims ------------------------------------------------
        prior_claims = [c for c in self.store.claims_for_customer(claim.customer_id)
                        if c.filed_at < claim.filed_at]
        recent = [c for c in prior_claims
                  if c.filed_at >= claim.filed_at - timedelta(days=180)]
        add("PRIOR_CLAIMS",
            f"{len(prior_claims)} lifetime claims, {len(recent)} in last 180d",
            {"lifetime": len(prior_claims), "last_180d": len(recent)})

        # -- tool 5: contact history (deterministic mock) -------------------------
        add("CONTACT_HISTORY", "No prior merchant-credit conversation on record",
            {"prior_merchant_credit_discussed": False})

        # -- CE 3.0 qualification (FRAUD_CNP only — critique finding #9) ----------
        if claim.network_category == NetworkCategory.FRAUD_CNP:
            hist = [t for t in prior
                    if t.merchant_id == txn.merchant_id and not t.disputed
                    and timedelta(days=120) <= (txn.occurred_at - t.occurred_at)
                    <= timedelta(days=365)]
            identity_link = device_known or ip_known or bool(oi and oi.device_match)
            b.ce3_qualified = len(hist) >= 2 and identity_link
            b.ce3_basis = (
                f"{len(hist)} undisputed transaction(s) at {txn.merchant_name} in the "
                f"120-365 day window; identity linkage "
                f"({'device/IP continuity' if identity_link else 'none'}). "
                f"{'QUALIFIES' if b.ce3_qualified else 'Does not qualify'} under "
                f"Visa Compelling Evidence 3.0.")
            add("CE3_ASSESSMENT", b.ce3_basis,
                {"qualified": b.ce3_qualified, "window_txns": len(hist),
                 "identity_link": identity_link})

        # -- recommendation from artifacts only -----------------------------------
        misuse_signals = sum([
            device_known, ip_known, tds.endswith("PASS"),
            bool(oi and oi.device_match), bool(oi and oi.delivery_confirmation),
            bool(oi and oi.prior_undisputed_orders >= 2), b.ce3_qualified,
            len(recent) >= 2])
        consumer_signals = sum([
            not device_known, not ip_known,
            bool(oi and oi.participating and not oi.order_found),
            txn.foreign, not txn.card_present and tds in ("FAILED", "NOT_ENROLLED")])

        if misuse_signals >= 4:
            b.recommendation, b.confidence = "CONFIRM_FIRST_PARTY", min(
                0.55 + 0.08 * misuse_signals, 0.97)
        elif consumer_signals >= 3 and misuse_signals <= 1:
            b.recommendation, b.confidence = "SUPPORT_CONSUMER", 0.8
        else:
            b.recommendation, b.confidence = "INCONCLUSIVE", 0.5

        b.narrative = self._draft_narrative(case, claim, txn, b)
        for item in b.items:
            self.store.put_evidence(item)
        return b

    @staticmethod
    def _draft_narrative(case: Case, claim: Claim, txn: Transaction,
                         b: EvidenceBundle) -> str:
        cites = {item.source: f"[E{i+1}]" for i, item in enumerate(b.items)}
        lines = [
            f"INVESTIGATOR NARRATIVE (DRAFT — requires human review) — case {case.id}",
            f"Claim {claim.id}: {claim.reason.value} for ${claim.amount:.2f} at "
            f"{txn.merchant_name}, transaction {txn.id} dated "
            f"{txn.occurred_at.date()}.",
        ]
        for i, item in enumerate(b.items):
            lines.append(f"[E{i+1}] {item.source}: {item.summary}")
        if b.ce3_basis:
            lines.append(f"CE 3.0: {b.ce3_basis} {cites.get('CE3_ASSESSMENT','')}")
        lines.append(
            f"Assessment: {b.recommendation} (confidence {b.confidence:.2f}), based on "
            f"the artifacts cited above. No customer-affecting action is taken from "
            f"this draft without checkpoint approval.")
        return "\n".join(lines)
