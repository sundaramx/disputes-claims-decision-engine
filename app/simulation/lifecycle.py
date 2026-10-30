"""Virtual-time lifecycle driver: generates a portfolio, trains the misuse model on
the bootstrap corpus, then advances the SimClock day by day feeding arrivals and
back-office work through the DisputeEngine. `LiveFeed` keeps the same world running
past the bootstrap horizon with open-ended arrivals (continuous simulation)."""
from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from app.domain.clock import SimClock
from app.decisioning.policy.engine import PolicyPack
from app.decisioning.scoring.model import MisuseModel, train_default_model
from app.engine import DisputeEngine
from app.simulation.generator import Portfolio, generate_portfolio, plan_claims_for_day
from app.store.db import Store

POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "adjudication_v1.yaml"
SIM_START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)


def run_simulation(seed: int = 42, n_customers: int = 600, n_claims: int = 900,
                   horizon_days: int = 80, db_path: str = ":memory:",
                   model: MisuseModel | None = None):
    rng = random.Random(seed)
    store = Store(db_path)
    clock = SimClock(SIM_START)

    portfolio = generate_portfolio(store, rng, SIM_START, n_customers=n_customers,
                                   n_claims=n_claims, horizon_days=horizon_days)
    model = model or train_default_model(seed=7)
    pack = PolicyPack(POLICY_PATH)
    participation = {m.id: m.order_insight for m in portfolio.merchants}
    engine = DisputeEngine(store, clock, pack, model, participation)

    plans_by_day: dict[int, list] = {}
    for p in portfolio.plans:
        plans_by_day.setdefault(p.day, []).append(p)

    for day in range(horizon_days):
        for plan in plans_by_day.get(day, []):
            engine.process_arrival(plan)
        engine.daily_tick()
        clock.advance(days=1)

    return store, engine, portfolio


class LiveFeed:
    """Continues a bootstrapped world one virtual day per `step()`: new arrivals at
    `claims_per_day` on average, then the engine's daily tick. Seeded, so the
    sequence of simulated days is reproducible (portal human actions aside)."""

    def __init__(self, store: Store, engine: DisputeEngine, portfolio: Portfolio,
                 claims_per_day: float, seed: int = 42):
        self.store, self.engine, self.portfolio = store, engine, portfolio
        self.claims_per_day = claims_per_day
        self.rng = random.Random(f"LIVE-{seed}")

    def step(self) -> int:
        """Advance one virtual day; returns the number of new claim plans."""
        lo = max(0, round(self.claims_per_day * 0.5))
        n = self.rng.randint(lo, round(self.claims_per_day * 1.5))
        plans = plan_claims_for_day(self.store, self.rng, self.portfolio, SIM_START,
                                    self.engine.day, n)
        for plan in plans:
            self.engine.process_arrival(plan)
        self.engine.daily_tick()
        self.engine.clock.advance(days=1)
        return n
