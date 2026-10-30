"""Rent-layer network utility adapters (paper §6: 'rent, always').

These mirror the interfaces of Verifi (Visa) and Ethoca (Mastercard) pre-dispute
utilities. The MOCK BACKENDS simulate the external world: their artifact contents
are truth-correlated (that is what real merchant data does), but the engine only
ever reads the artifact fields — never the simulation's ground-truth label.
Everything is seeded per-transaction for deterministic replay.

Production swap-in: replace `MockNetworkBackend` with real API clients; the
adapter interfaces (`order_insight_lookup`, `consumer_clarity_lookup`) are stable.
RDR / CDRN / Mastercom are documented stubs (PLAN.md §15.14).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from app.domain.enums import TrueType
from app.domain.models import Claim, Transaction


@dataclass
class OrderInsightResponse:
    participating: bool
    order_found: bool = False
    device_match: bool = False
    address_match: bool = False
    prior_undisputed_orders: int = 0        # CE 3.0 evidence carried by Order Insight
    item_description: str = ""
    delivery_confirmation: bool = False


class MockNetworkBackend:
    """Simulates merchant-side data quality. `participation` maps merchant_id ->
    whether the merchant is enrolled in Order Insight / Consumer Clarity."""

    def __init__(self, participation: dict[str, bool]):
        self.participation = participation

    def order_insight_lookup(self, claim: Claim, txn: Transaction,
                             evidence_quality: float) -> OrderInsightResponse:
        rng = random.Random(f"OI-{txn.id}")
        if not self.participation.get(txn.merchant_id, False):
            return OrderInsightResponse(participating=False)
        first_party = claim.true_type == TrueType.FIRST_PARTY_MISUSE
        # merchant data surfaces the truth with probability ~ evidence_quality
        reveals = rng.random() < evidence_quality
        return OrderInsightResponse(
            participating=True,
            order_found=rng.random() < 0.9,
            device_match=first_party and reveals if rng.random() > 0.1
                         else rng.random() < 0.15,
            address_match=(first_party or claim.true_type == TrueType.MERCHANT_ERROR)
                          and reveals,
            prior_undisputed_orders=rng.randint(2, 5) if (first_party and reveals)
                                    else rng.randint(0, 1),
            item_description=f"Order at {txn.merchant_name}",
            delivery_confirmation=first_party and rng.random() < evidence_quality)

    # Ethoca Consumer Clarity has the same shape for our purposes
    consumer_clarity_lookup = order_insight_lookup


def rdr_stub() -> None:
    """Verifi RDR / CDRN and Mastercom: out of scope; connect-only interfaces in a
    real deployment. Kept as an explicit stub so the sourcing decision is visible."""
    raise NotImplementedError("Rent from network; not simulated in this build")
