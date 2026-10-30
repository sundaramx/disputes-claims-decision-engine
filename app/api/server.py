"""Operations API + portal server.

Boots the virtual-time simulation once at startup (seeded, deterministic), then
keeps it running live: a background thread advances one virtual day every
SIM_TICK_SECONDS with fresh arrivals, for SIM_LIVE_SECONDS of wall-clock time
(0 disables the live run). Every API request is serialized against the ticker so
reads always see a consistent day. The human
checkpoint endpoints keep working live: pending-review cases can be approved or
denied from the portal, flowing through the same adjudicator + ledger path the
simulation used.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.chassis import clocks as clk
from app.decisioning.learning.metrics import compute_kpis, retrain_from_outcomes
from app.domain.enums import CaseStatus, ClockState, DecisionAction
from app.simulation.lifecycle import LiveFeed, run_simulation

PORTAL = Path(__file__).resolve().parents[1] / "portal" / "static"

state: dict = {}
LOCK = threading.Lock()     # one engine mutation or API read at a time


class LiveRunner:
    """Background ticker driving the LiveFeed for a bounded wall-clock window."""

    def __init__(self, feed: LiveFeed, tick_seconds: float, run_seconds: float):
        self.feed, self.tick_seconds, self.run_seconds = feed, tick_seconds, run_seconds
        self.deadline = 0.0
        self.days_run = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @property
    def running(self) -> bool:
        return time.monotonic() < self.deadline

    def start(self) -> None:
        self.resume()
        self._thread.start()

    def resume(self) -> None:
        self.deadline = time.monotonic() + self.run_seconds
        self._wake.set()

    def pause(self) -> None:
        self.deadline = 0.0

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)

    def status(self) -> dict:
        return {"running": self.running, "days_run": self.days_run,
                "tick_seconds": self.tick_seconds, "run_seconds": self.run_seconds,
                "seconds_left": round(max(0.0, self.deadline - time.monotonic()), 1),
                "virtual_now": self.feed.engine.clock.now().isoformat()}

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.running:
                self._wake.clear()
                self._wake.wait()
                continue
            with LOCK:
                self.feed.step()
                self.days_run += 1
            self._stop.wait(self.tick_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed = int(os.environ.get("SIM_SEED", "42"))
    n_claims = int(os.environ.get("SIM_CLAIMS", "900"))
    horizon = int(os.environ.get("SIM_DAYS", "80"))
    store, engine, portfolio = run_simulation(
        seed=seed, n_customers=int(os.environ.get("SIM_CUSTOMERS", "600")),
        n_claims=n_claims, horizon_days=horizon)
    state.update(store=store, engine=engine, portfolio=portfolio)
    runner = None
    run_seconds = float(os.environ.get("SIM_LIVE_SECONDS", "60"))
    if run_seconds > 0:
        feed = LiveFeed(store, engine, portfolio, claims_per_day=n_claims / horizon,
                        seed=seed)
        runner = LiveRunner(feed, float(os.environ.get("SIM_TICK_SECONDS", "2")),
                            run_seconds)
        runner.start()
    state["runner"] = runner
    yield
    if runner:
        runner.shutdown()
    state.clear()


def _serialized():
    with LOCK:
        yield


app = FastAPI(title="Disputes & Claims Decision Engine", lifespan=lifespan,
              dependencies=[Depends(_serialized)])


def _case_summary(case, store, now):
    claim = store.get_claim(case.claim_id)
    worst = None
    for k in case.clocks:
        if k.state in (ClockState.RUNNING, ClockState.AT_RISK, ClockState.BREACHED):
            days = (k.due_at - now).total_seconds() / 86400
            if worst is None or days < worst["days_remaining"]:
                worst = {"clock": k.clock_type.value, "state": k.state.value,
                         "due_at": k.due_at.isoformat(),
                         "days_remaining": round(days, 1)}
    return {
        "id": case.id, "claim_id": case.claim_id, "status": case.status.value,
        "queue": case.assigned_queue, "regime": case.regime.value,
        "tier": case.tier.value if case.tier else None,
        "cohort": case.cohort.value if case.cohort else None,
        "misuse_score": round(case.misuse_score, 3) if case.misuse_score is not None
                        else None,
        "amount": claim.amount, "channel": claim.channel.value,
        "reason": claim.reason.value, "opened_at": case.opened_at.isoformat(),
        "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None,
        "escalated": case.escalated, "touches": case.touches,
        "binding_clock": worst,
        "pc_posted": case.provisional_credit.posted_at is not None,
    }


@app.get("/api/kpis")
def kpis():
    engine, store = state["engine"], state["store"]
    k = compute_kpis(store, engine.model, engine.adjudicator.live_scores,
                     engine.clock.now())
    k["experiments"]["policy_pack"] = engine.pack.to_dict()
    return k


@app.get("/api/cases")
def cases(status: str | None = None, queue: str | None = None,
          open_only: bool = False, at_risk: bool = False, limit: int = 200):
    store, engine = state["store"], state["engine"]
    now = engine.clock.now()
    out = []
    for c in store.cases(status=status, queue=queue):
        if open_only and c.resolved_at is not None:
            continue
        if at_risk and not any(k.state in (ClockState.AT_RISK, ClockState.BREACHED)
                               for k in c.clocks):
            continue
        out.append(_case_summary(c, store, now))
    out.sort(key=lambda s: (s["binding_clock"] or {}).get("days_remaining", 1e9))
    return {"now": now.isoformat(), "count": len(out), "cases": out[:limit]}


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str):
    store, engine = state["store"], state["engine"]
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    claim = store.get_claim(case.claim_id)
    txn = store.get_transaction(claim.transaction_id)
    outcome = store.get_outcome(case_id)
    return {
        "summary": _case_summary(case, store, engine.clock.now()),
        "claim": claim.model_dump(mode="json", exclude={"true_type"}),
        "transaction": txn.model_dump(mode="json"),
        "score_reasons": case.score_reasons,
        "clocks": [c.model_dump(mode="json") for c in case.clocks],
        "provisional_credit": case.provisional_credit.model_dump(mode="json"),
        "reg_z_pendency": case.reg_z_pendency.model_dump(mode="json"),
        "decisions": [d.model_dump(mode="json")
                      for d in store.decisions_for_case(case_id)],
        "evidence": [e.model_dump(mode="json")
                     for e in store.evidence_for_case(case_id)],
        # the examiner view: the timeline below is rendered from the ledger ALONE
        "ledger": [{"seq": e.seq, "ts": e.ts.isoformat(), "event_type": e.event_type,
                    "payload": e.payload, "hash": e.hash[:16] + "…"}
                   for e in store.ledger_entries(case_id)],
        "outcome": outcome.model_dump(mode="json") if outcome else None,
    }


class ReviewAction(BaseModel):
    action: str            # "approve_pay" | "approve_denial"
    rationale: str
    reviewer: str = "portal.user"


@app.post("/api/cases/{case_id}/review")
def review(case_id: str, body: ReviewAction):
    store, engine = state["store"], state["engine"]
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    if case.status != CaseStatus.PENDING_REVIEW:
        raise HTTPException(409, f"case is {case.status.value}, not PENDING_REVIEW")
    action = (DecisionAction.APPROVE_PAY if body.action == "approve_pay"
              else DecisionAction.APPROVE_DENIAL)
    claim = store.get_claim(case.claim_id)
    account = store.get_account(case.account_id)
    d = engine.adjudicator.human_action(engine.clock.now(), case, action,
                                        body.rationale, body.reviewer)
    engine._execute(d, case, claim, account)
    engine._maybe_close(case, claim)
    # drop from the pending review queue if simulated work is still holding it
    engine.review_queue = [w for w in engine.review_queue if w.case_id != case_id]
    return {"ok": True, "case": _case_summary(store.get_case(case_id), store,
                                              engine.clock.now())}


@app.get("/api/ledger/verify")
def ledger_verify():
    return state["engine"].ledger.verify()


@app.get("/api/policy")
def policy():
    return state["engine"].pack.to_dict()


@app.get("/api/model")
def model():
    return state["engine"].model.to_dict()


@app.post("/api/retrain")
def retrain():
    return retrain_from_outcomes(state["store"], state["engine"].model)


@app.get("/api/sim")
def sim_status():
    runner = state.get("runner")
    if runner is None:
        return {"enabled": False, "running": False,
                "virtual_now": state["engine"].clock.now().isoformat()}
    return {"enabled": True, **runner.status()}


@app.post("/api/sim/{action}")
def sim_control(action: str):
    runner = state.get("runner")
    if runner is None:
        raise HTTPException(409, "live simulation disabled (SIM_LIVE_SECONDS=0)")
    if action == "resume":
        runner.resume()
    elif action == "pause":
        runner.pause()
    else:
        raise HTTPException(404, "action must be 'resume' or 'pause'")
    return {"enabled": True, **runner.status()}


@app.get("/")
def index():
    return FileResponse(PORTAL / "index.html")


app.mount("/static", StaticFiles(directory=PORTAL), name="static")
