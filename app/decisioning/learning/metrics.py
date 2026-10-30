"""KPI catalog computation — the operations portal's data source.

Every KPI here is computable from the store alone (plus the model for drift), which
is exactly the paper's Phase 1 demand: instrument the baseline before claiming
improvement (§8). Definitions live next to their computation on purpose — an ops
review should be able to audit both together.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app.decisioning.scoring.model import MisuseModel
from app.domain.enums import (CaseStatus, ClockState, ClockType, Cohort,
                              PaymentChannel, RegRegime, Tier)
from app.store.db import Store

CARD = (PaymentChannel.CARD_CREDIT, PaymentChannel.CARD_DEBIT)


def _pct(n: float, d: float) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _median_p90(vals: list[float]) -> tuple[float | None, float | None]:
    if not vals:
        return None, None
    vs = sorted(vals)
    return (round(statistics.median(vs), 1),
            round(vs[min(int(0.9 * len(vs)), len(vs) - 1)], 1))


def compute_kpis(store: Store, model: MisuseModel, live_scores: list[float],
                 now: datetime) -> dict:
    cases = store.all_cases()
    outcomes = {o.case_id: o for o in store.all_outcomes()}
    claims = {c.id: store.get_claim(c.claim_id) for c in cases}
    inquiries = store.all_inquiries()
    open_cases = [c for c in cases if c.resolved_at is None]
    closed = [c for c in cases if c.resolved_at is not None and c.id in outcomes]

    rege = [c for c in cases if c.regime == RegRegime.REG_E]
    regz = [c for c in cases if c.regime == RegRegime.REG_Z]

    # ---------------- compliance ----------------
    def _clock(c, ct):
        for k in c.clocks:
            if k.clock_type == ct:
                return k
        return None

    pc_due = [c for c in rege
              if c.resolved_at is None or c.provisional_credit.posted_at
              or (c.resolved_at and _clock(c, ClockType.REG_E_INVESTIGATE)
                  and c.resolved_at > _clock(c, ClockType.REG_E_INVESTIGATE).due_at)]
    pc_posted = [c for c in rege if c.provisional_credit.posted_at]
    pc_on_time = [c for c in pc_posted
                  if c.provisional_credit.posted_at
                  <= _clock(c, ClockType.REG_E_INVESTIGATE).due_at]

    breached_clocks = Counter()
    at_risk_now = []
    for c in cases:
        for k in c.clocks:
            if k.state == ClockState.BREACHED:
                breached_clocks[k.clock_type.value] += 1
        if c.resolved_at is None and any(k.state == ClockState.AT_RISK
                                         for k in c.clocks):
            at_risk_now.append(c.id)

    rege_resolved_on_time = [
        c for c in rege if c.resolved_at is not None
        and not any(k.state == ClockState.BREACHED and k.clock_type in
                    (ClockType.REG_E_INVESTIGATE, ClockType.REG_E_RESOLVE)
                    for k in c.clocks)]
    regz_resolved_on_time = [
        c for c in regz if c.resolved_at is not None
        and not any(k.state == ClockState.BREACHED and
                    k.clock_type == ClockType.REG_Z_RESOLVE for k in c.clocks)]

    pendency_ok = [c for c in regz if c.reg_z_pendency.activated_at is not None]
    interest_paid = round(sum(c.provisional_credit.interest_accrued for c in cases), 2)

    # ---------------- efficiency ----------------
    stp = [c for c in closed if c.touches == 0]
    t0 = [c for c in cases if c.tier == Tier.T0_AUTO_PAY]
    cycle_days = [((c.resolved_at - c.opened_at).total_seconds() / 86400)
                  for c in closed]
    med_cycle, p90_cycle = _median_p90(cycle_days)
    cycle_by_regime = {}
    for regime in RegRegime:
        vals = [(c.resolved_at - c.opened_at).total_seconds() / 86400
                for c in closed if c.regime == regime]
        m, p = _median_p90(vals)
        cycle_by_regime[regime.value] = {"median": m, "p90": p, "n": len(vals)}

    queue_depth = Counter(c.assigned_queue for c in open_cases)
    aging = {"0-5d": 0, "6-15d": 0, "16-30d": 0, "30d+": 0}
    for c in open_cases:
        age = (now - c.opened_at).days
        key = ("0-5d" if age <= 5 else "6-15d" if age <= 15
               else "16-30d" if age <= 30 else "30d+")
        aging[key] += 1

    deflected = [i for i in inquiries if i.deflected]
    converted = [i for i in inquiries if not i.deflected]

    # ---------------- loss & recovery ----------------
    paid_amount = sum(o.loss_amount + o.recovered_amount
                      for o in outcomes.values() if o.resolution == "PAID")
    net_loss = round(sum(o.loss_amount for o in outcomes.values()), 2)
    recovered = round(sum(o.recovered_amount for o in outcomes.values()), 2)
    reps = [o for o in outcomes.values() if o.representment_filed]
    reps_decided = [o for o in reps if o.representment_won is not None]
    reps_won = [o for o in reps_decided if o.representment_won]
    late_forfeits = [o for o in outcomes.values()
                     if o.recovery_forfeited_late_filing]
    denials = [o for o in outcomes.values() if o.resolution == "DENIED"]
    reopened = [o for o in outcomes.values() if o.reopened]
    flagged = [c for c in cases if c.tier in (Tier.T2_ENHANCED_INVESTIGATION,
                                              Tier.T3_DEFENDED_DENIAL_CANDIDATE)]

    # ---------------- model & policy health ----------------
    labeled = [(c.misuse_score, outcomes[c.id].label_first_party)
               for c in closed if c.misuse_score is not None
               and outcomes[c.id].label_first_party is not None
               and not outcomes[c.id].reopened]
    live_auc = _auc(labeled)
    calibration = _calibration(labeled)
    # drift is computed from the store (reproducible without engine state):
    # scores ordered by case open time; recent = last 21 days of scored traffic
    scored_cases = sorted((c for c in cases if c.misuse_score is not None),
                          key=lambda c: c.opened_at)
    live_scores = [c.misuse_score for c in scored_cases]
    psi = model.psi(live_scores)
    recent_scores = [c.misuse_score for c in scored_cases
                     if c.opened_at >= now - timedelta(days=21)]
    psi_within_run = model.psi(recent_scores) if recent_scores else 0.0

    hist = [0] * 10
    for s in live_scores:
        hist[min(int(s * 10), 9)] += 1

    cohorts = {}
    for coh in Cohort:
        cc = [c for c in closed if c.cohort == coh]
        oo = [outcomes[c.id] for c in cc]
        amt = sum(claims[c.id].amount for c in cc) or 1
        cohorts[coh.value] = {
            "cases": len(cc),
            "stp_rate": _pct(sum(1 for c in cc if c.touches == 0), len(cc)),
            "avg_touches": round(sum(c.touches for c in cc) / len(cc), 2) if cc else None,
            "net_loss": round(sum(o.loss_amount for o in oo), 2),
            "loss_rate_bps": round(10000 * sum(o.loss_amount for o in oo) / amt, 0),
            "denial_rate": _pct(sum(1 for o in oo if o.resolution == "DENIED"),
                                len(oo)),
            "reopen_rate": _pct(sum(1 for o in oo if o.reopened), len(oo)),
        }

    # ---------------- weekly trend ----------------
    trend = defaultdict(lambda: {"filed": 0, "stp": 0, "closed": 0, "net_loss": 0.0,
                                 "breaches": 0})
    for c in cases:
        wk = f"W{(c.opened_at - min(x.opened_at for x in cases)).days // 7 + 1}"
        trend[wk]["filed"] += 1
        if c.touches == 0 and c.resolved_at is not None:
            trend[wk]["stp"] += 1
        if c.id in outcomes:
            trend[wk]["closed"] += 1
            trend[wk]["net_loss"] += outcomes[c.id].loss_amount
        trend[wk]["breaches"] += sum(1 for k in c.clocks
                                     if k.state == ClockState.BREACHED)
    trend = {k: {**v, "net_loss": round(v["net_loss"], 2),
                 "stp_rate": _pct(v["stp"], v["filed"])}
             for k, v in sorted(trend.items(), key=lambda kv: int(kv[0][1:]))}

    return {
        "as_of": now.isoformat(),
        "portfolio": {
            "total_claims": len(cases), "open_cases": len(open_cases),
            "closed_cases": len(closed),
            "reg_e": len(rege), "reg_z": len(regz),
            "by_channel": dict(Counter(claims[c.id].channel.value for c in cases)),
            "by_tier": dict(Counter(c.tier.value for c in cases if c.tier)),
        },
        "compliance": {
            "pc_posted": len(pc_posted),
            "pc_on_time_rate": _pct(len(pc_on_time), len(pc_posted)),
            "reg_e_resolved_on_time_rate": _pct(
                len(rege_resolved_on_time),
                len([c for c in rege if c.resolved_at is not None])),
            "reg_z_resolved_on_time_rate": _pct(
                len(regz_resolved_on_time),
                len([c for c in regz if c.resolved_at is not None])),
            "reg_z_pendency_activation_rate": _pct(len(pendency_ok), len(regz)),
            "clock_breaches": dict(breached_clocks),
            # regulatory (Reg E / Reg Z) vs network deadlines are different failures:
            # one is a strict-liability compliance violation, the other forfeits money
            "regulatory_breaches": sum(v for k, v in breached_clocks.items()
                                       if k.startswith("REG_")),
            "network_breaches": sum(v for k, v in breached_clocks.items()
                                    if not k.startswith("REG_")),
            "total_breaches": sum(breached_clocks.values()),
            "at_risk_now": len(at_risk_now),
            "at_risk_case_ids": at_risk_now[:50],
            "pc_interest_paid": interest_paid,
        },
        "efficiency": {
            "stp_rate": _pct(len(stp), len(closed)),
            "t0_share": _pct(len(t0), len(cases)),
            "avg_touches": round(sum(c.touches for c in closed) / len(closed), 2)
                           if closed else None,
            "median_cycle_days": med_cycle, "p90_cycle_days": p90_cycle,
            "cycle_by_regime": cycle_by_regime,
            "queue_depth": dict(queue_depth), "aging": aging,
            "deflection_rate": _pct(len(deflected), len(inquiries)),
            "inquiries": len(inquiries), "deflected": len(deflected),
        },
        "loss_recovery": {
            "paid_to_consumers": round(paid_amount, 2),
            "net_loss": net_loss, "recovered": recovered,
            "recovery_rate": _pct(recovered, paid_amount),
            "representments_filed": len(reps),
            "representment_win_rate": _pct(len(reps_won), len(reps_decided)),
            "representments_pending": len(reps) - len(reps_decided),
            "late_filing_forfeits": len(late_forfeits),
            "denial_rate": _pct(len(denials), len(outcomes)),
            "first_party_flagged_rate": _pct(len(flagged), len(cases)),
            "denials_upheld_rate": _pct(
                len([o for o in denials if not o.reopened]), len(denials)),
        },
        "customer": {
            "avg_days_to_pc": round(statistics.mean(
                [(c.provisional_credit.posted_at - c.opened_at).days
                 for c in pc_posted]), 1) if pc_posted else None,
            "reopen_rate": _pct(len(reopened), len(outcomes)),
        },
        "model": {
            "version": model.version, "training_auc": model.auc,
            "trained_on": model.trained_on,
            "live_auc_on_closed_cases": live_auc,
            "psi_vs_training": psi,
            "psi_late_vs_training": psi_within_run,
            # standard PSI bands: <0.05 stable, 0.05-0.10 watch, >=0.10 alert
            "drift_status": ("ALERT" if psi_within_run >= 0.10 else
                             "WATCH" if psi_within_run >= 0.05 else "STABLE"),
            "drift_alert": psi_within_run >= 0.10,
            "score_histogram": hist,
            "calibration": calibration,
            "scored_cases": len(live_scores),
        },
        "experiments": {"policy_pack": None, "cohorts": cohorts},
        "trend": trend,
        "context": {
            "vamp_note": ("Visa VAMP 0.9% 'excessive' threshold (Jan 2026) is a "
                          "merchant/acquirer-side program metric — shown as merchant-"
                          "risk context, not an issuer KPI.")},
    }


def _auc(pairs: list[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    pos = sum(1 for _, y in pairs if y)
    neg = len(pairs) - pos
    if not pos or not neg:
        return None
    ranked = sorted(pairs, key=lambda p: p[0])
    rank_sum = sum(i + 1 for i, (_, y) in enumerate(ranked) if y)
    return round((rank_sum - pos * (pos + 1) / 2) / (pos * neg), 3)


def _calibration(pairs: list[tuple[float, bool]], bins: int = 5) -> list[dict]:
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(s, y) for s, y in pairs if lo <= s < hi or (b == bins - 1 and s == 1)]
        if sel:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}",
                        "predicted": round(sum(s for s, _ in sel) / len(sel), 3),
                        "actual": round(sum(1 for _, y in sel if y) / len(sel), 3),
                        "n": len(sel)})
    return out


def retrain_from_outcomes(store: Store, base_model: MisuseModel) -> dict:
    """Learning loop: closed cases become training labels; a candidate model is
    trained on them and compared to the incumbent (champion model governance)."""
    from app.decisioning.features.extract import as_vector, extract_features

    X, y = [], []
    outcomes = {o.case_id: o for o in store.all_outcomes()}
    for case in store.all_cases():
        o = outcomes.get(case.id)
        if o is None or o.label_first_party is None or o.reopened:
            continue
        claim = store.get_claim(case.claim_id)
        cust = store.get_customer(case.customer_id)
        acct = store.get_account(case.account_id)
        txn = store.get_transaction(claim.transaction_id)
        X.append(as_vector(extract_features(store, claim, cust, acct, txn)))
        y.append(1 if o.label_first_party else 0)
    if len(y) < 50 or len(set(y)) < 2:
        return {"retrained": False, "reason": "insufficient labeled outcomes"}
    candidate = MisuseModel()
    candidate.train(X, y)
    return {"retrained": True, "candidate_version": candidate.version,
            "candidate_auc": candidate.auc, "incumbent_version": base_model.version,
            "incumbent_auc": base_model.auc, "labeled_cases": len(y),
            "recommendation": ("PROMOTE candidate to challenger slot"
                               if candidate.auc > base_model.auc else
                               "RETAIN incumbent; candidate does not beat it")}
