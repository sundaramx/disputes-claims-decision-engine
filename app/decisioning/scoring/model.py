"""First-party misuse scorer.

A deliberately transparent model: standardized logistic regression trained by
gradient descent on outcome-labeled historical cases (the paper's compounding data
asset, §4/§7). Every score ships with signed reason codes (top feature
contributions) and a pinned model version; the score is an INPUT to policy-as-code,
never a decision by itself (paper §4 principle).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field

from app.decisioning.features.extract import FEATURE_NAMES

LABEL_NOISE = 0.06     # honest training labels: some closed-case labels are wrong


@dataclass
class MisuseModel:
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    means: list[float] = field(default_factory=list)
    stds: list[float] = field(default_factory=list)
    version: str = "untrained"
    trained_on: int = 0
    auc: float = 0.0
    # reference score distribution (deciles) for PSI drift monitoring
    reference_deciles: list[float] = field(default_factory=list)

    # ---------------- training ----------------
    def train(self, X: list[list[float]], y: list[int], epochs: int = 300,
              lr: float = 0.05, l2: float = 1e-3) -> None:
        n, d = len(X), len(X[0])
        self.means = [sum(row[j] for row in X) / n for j in range(d)]
        self.stds = [max(math.sqrt(sum((row[j] - self.means[j]) ** 2 for row in X) / n),
                         1e-6) for j in range(d)]
        Z = [self._standardize(row) for row in X]
        w, b = [0.0] * d, 0.0
        for _ in range(epochs):
            gw, gb = [0.0] * d, 0.0
            for zi, yi in zip(Z, y):
                p = _sigmoid(sum(wj * zj for wj, zj in zip(w, zi)) + b)
                err = p - yi
                for j in range(d):
                    gw[j] += err * zi[j]
                gb += err
            for j in range(d):
                w[j] -= lr * (gw[j] / n + l2 * w[j])
            b -= lr * gb / n
        self.weights, self.bias, self.trained_on = w, b, n
        self.auc = self._auc(Z, y)
        scores = sorted(self._score_z(z) for z in Z)
        self.reference_deciles = [scores[int(k * (n - 1) / 10)] for k in range(11)]
        blob = json.dumps({"w": w, "b": b, "m": self.means, "s": self.stds})
        self.version = "misuse-lr-" + hashlib.sha256(blob.encode()).hexdigest()[:10]

    # ---------------- inference ----------------
    def score(self, features: dict[str, float]) -> float:
        z = self._standardize([features[n] for n in FEATURE_NAMES])
        return self._score_z(z)

    def reasons(self, features: dict[str, float], top: int = 4) -> list[dict]:
        """Signed contributions of standardized features — the reason codes that go
        into the decision ledger and the investigator view."""
        z = self._standardize([features[n] for n in FEATURE_NAMES])
        contribs = [(FEATURE_NAMES[j], self.weights[j] * z[j]) for j in range(len(z))]
        contribs.sort(key=lambda kv: abs(kv[1]), reverse=True)
        return [{"feature": name, "contribution": round(c, 4),
                 "direction": "raises_misuse_risk" if c > 0 else "lowers_misuse_risk",
                 "value": features[name]} for name, c in contribs[:top]]

    def psi(self, live_scores: list[float]) -> float:
        """Population stability index of live scores vs. training reference deciles."""
        if not live_scores or len(self.reference_deciles) < 11:
            return 0.0
        edges = self.reference_deciles
        n = len(live_scores)
        psi = 0.0
        for k in range(10):
            lo, hi = edges[k], edges[k + 1]
            if k == 0:
                actual = sum(1 for s in live_scores if s <= hi) / n
            elif k == 9:
                actual = sum(1 for s in live_scores if s > lo) / n
            else:
                actual = sum(1 for s in live_scores if lo < s <= hi) / n
            expected, actual = 0.10, max(actual, 1e-4)
            psi += (actual - expected) * math.log(actual / expected)
        return round(psi, 4)

    # ---------------- internals ----------------
    def _standardize(self, row: list[float]) -> list[float]:
        return [(row[j] - self.means[j]) / self.stds[j] for j in range(len(row))]

    def _score_z(self, z: list[float]) -> float:
        return _sigmoid(sum(w * v for w, v in zip(self.weights, z)) + self.bias)

    def _auc(self, Z, y) -> float:
        pairs = sorted(zip((self._score_z(z) for z in Z), y))
        pos = sum(y)
        neg = len(y) - pos
        if pos == 0 or neg == 0:
            return 0.5
        rank_sum = sum(i + 1 for i, (_, yi) in enumerate(pairs) if yi == 1)
        return round((rank_sum - pos * (pos + 1) / 2) / (pos * neg), 4)

    def to_dict(self) -> dict:
        return {"version": self.version, "trained_on": self.trained_on, "auc": self.auc,
                "weights": dict(zip(FEATURE_NAMES, [round(w, 4) for w in self.weights])),
                "bias": round(self.bias, 4)}


def _sigmoid(x: float) -> float:
    if x < -35:
        return 0.0
    if x > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def build_training_corpus(seed: int = 7, n_claims: int = 700):
    """Bootstrap corpus: a scratch portfolio standing in for historical closed cases.
    Labels get LABEL_NOISE flips — closed-case labels are imperfect in real life too."""
    from datetime import datetime, timezone

    from app.domain.enums import TrueType
    from app.simulation.generator import generate_portfolio
    from app.store.db import Store
    from app.decisioning.features.extract import as_vector, extract_features

    rng = random.Random(seed)
    store = Store(":memory:")
    sim_start = datetime(2025, 6, 1, tzinfo=timezone.utc)
    p = generate_portfolio(store, rng, sim_start, n_customers=350, n_claims=n_claims,
                           horizon_days=80)
    X, y = [], []
    for plan in p.plans:
        claim = plan.claim
        store.put_claim(claim)  # so later claims see earlier ones as history
        cust = store.get_customer(claim.customer_id)
        acct = store.get_account(claim.account_id)
        txn = store.get_transaction(claim.transaction_id)
        feats = extract_features(store, claim, cust, acct, txn)
        label = 1 if claim.true_type == TrueType.FIRST_PARTY_MISUSE else 0
        if rng.random() < LABEL_NOISE:
            label = 1 - label
        X.append(as_vector(feats))
        y.append(label)
    return X, y


def train_default_model(seed: int = 7) -> MisuseModel:
    X, y = build_training_corpus(seed=seed)
    m = MisuseModel()
    m.train(X, y)
    return m
