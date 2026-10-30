"""Policy-as-code engine.

Loads versioned YAML policy packs, assigns champion/challenger cohorts
deterministically, and evaluates the arbitration ladder. Every evaluation returns a
full rule trace for the decision ledger. The engine also owns the non-negotiable
INVARIANTS that sit OUTSIDE any policy pack and bind every cohort:

  INV-NO-AUTO-DENIAL   no automated action may deny a claim
  INV-PC-GUARANTEE     Reg E provisional credit posts by the investigate deadline
                       if the case is unresolved (executed via adjudicator decision)
  INV-REGZ-PENDENCY    Reg Z pendency protections activate at intake
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.domain.enums import Cohort, DecisionAction, Tier

FORBIDDEN_AUTOMATED_ACTIONS = {DecisionAction.APPROVE_DENIAL,
                               DecisionAction.REVERSE_PROVISIONAL_CREDIT}


class PolicyViolation(Exception):
    pass


@dataclass
class TierRule:
    id: str
    description: str
    max_misuse_score: float
    max_amount: float | None = None
    max_claims_180d: int | None = None
    resolve_consumer: str = "PROVISIONAL_BY_DEADLINE"
    recovery: str = "EVALUATE_AFTER_INVESTIGATION"
    recovery_min_amount: float = 25.0


@dataclass
class TierEvaluation:
    tier: Tier
    rule: TierRule
    cohort: Cohort
    pack_id: str
    pack_version: str
    trace: list[str] = field(default_factory=list)


class PolicyPack:
    def __init__(self, path: str | Path):
        raw = yaml.safe_load(Path(path).read_text())
        self.id: str = raw["id"]
        self.version: str = str(raw["version"])
        self.challenger_share: int = int(raw.get("challenger_share", 0))
        self.cohorts: dict[Cohort, list[TierRule]] = {}
        for cohort_name, body in raw["cohorts"].items():
            rules = [TierRule(
                id=t["id"], description=t.get("description", ""),
                max_misuse_score=float(t["max_misuse_score"]),
                max_amount=t.get("max_amount"),
                max_claims_180d=t.get("max_claims_180d"),
                resolve_consumer=t.get("resolve_consumer", "PROVISIONAL_BY_DEADLINE"),
                recovery=t.get("recovery", "EVALUATE_AFTER_INVESTIGATION"),
                recovery_min_amount=float(t.get("recovery_min_amount", 25.0)),
            ) for t in body["tiers"]]
            self.cohorts[Cohort(cohort_name)] = rules
        self._validate()

    def _validate(self) -> None:
        """A policy pack can never smuggle in an automated denial: the schema has no
        deny action, and resolve_consumer values are whitelisted."""
        allowed = {"PAY_FINAL", "PROVISIONAL_NOW", "PROVISIONAL_BY_DEADLINE"}
        for cohort, rules in self.cohorts.items():
            for r in rules:
                if r.resolve_consumer not in allowed:
                    raise PolicyViolation(
                        f"{self.id}@{self.version} {cohort.value}/{r.id}: "
                        f"resolve_consumer '{r.resolve_consumer}' is not permitted "
                        f"(INV-NO-AUTO-DENIAL)")

    def assign_cohort(self, case_id: str) -> Cohort:
        """Deterministic, seedable, replay-stable cohort assignment."""
        bucket = int(hashlib.sha256(case_id.encode()).hexdigest()[:8], 16) % 100
        return Cohort.CHALLENGER if bucket < self.challenger_share else Cohort.CHAMPION

    def evaluate(self, case_id: str, amount: float, misuse_score: float,
                 claims_180d: int, cohort: Cohort | None = None) -> TierEvaluation:
        cohort = cohort or self.assign_cohort(case_id)
        trace: list[str] = [f"cohort={cohort.value} (deterministic hash of case id, "
                            f"challenger_share={self.challenger_share}%)"]
        for rule in self.cohorts[cohort]:
            checks: list[str] = []
            ok = True
            if rule.max_amount is not None:
                passed = amount <= rule.max_amount
                checks.append(f"amount {amount:.2f} <= {rule.max_amount}: {passed}")
                ok &= passed
            if rule.max_claims_180d is not None:
                passed = claims_180d <= rule.max_claims_180d
                checks.append(f"claims_180d {claims_180d} <= {rule.max_claims_180d}: "
                              f"{passed}")
                ok &= passed
            passed = misuse_score <= rule.max_misuse_score
            checks.append(f"misuse_score {misuse_score:.3f} <= "
                          f"{rule.max_misuse_score}: {passed}")
            ok &= passed
            trace.append(f"{rule.id}: {'MATCH' if ok else 'no'} ({'; '.join(checks)})")
            if ok:
                return TierEvaluation(tier=Tier(rule.id), rule=rule, cohort=cohort,
                                      pack_id=self.id, pack_version=self.version,
                                      trace=trace)
        raise PolicyViolation("No tier matched — ladder must be total")

    def to_dict(self) -> dict:
        return {"id": self.id, "version": self.version,
                "challenger_share": self.challenger_share,
                "cohorts": {c.value: [vars(r) for r in rules]
                            for c, rules in self.cohorts.items()}}


def assert_automated_action_permitted(action: DecisionAction) -> None:
    """INV-NO-AUTO-DENIAL: hard engine gate, independent of any policy pack."""
    if action in FORBIDDEN_AUTOMATED_ACTIONS:
        raise PolicyViolation(
            f"Automated actor attempted forbidden action {action.value} "
            f"(INV-NO-AUTO-DENIAL)")
