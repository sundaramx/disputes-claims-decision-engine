# Intelligent Disputes & Claims — Decision Engine + Operations Portal

A working reference implementation of the paper *Intelligent Disputes and Claims: An
AI-Enabled Operating Model for the Dispute Lifecycle at a Large Card Issuer*
(`Intelligent_Disputes_and_Claims.docx`). It builds the layer the paper says a large
issuer should **build** — the AI decisioning layer and data foundation — around a
lightweight case-management chassis, with the network utilities mocked behind
rent-layer adapter interfaces.

## What it demonstrates

| Paper claim | Where it lives |
|---|---|
| Timeline compliance as a **system property** (Reg E 10/20bd, conditional 45/90d, Reg Z 30d + 2-cycle/90d, network windows) | `app/chassis/clocks.py`, `app/chassis/calendar.py` |
| Provisional credit as a guarantee, executed as a **ledgered policy decision** (never a silent side-effect); Reg Z pendency protections instead of PC | `Adjudicator.on_clock_event` (INV-PC-GUARANTEE), `chassis/workflow.py` |
| First-party misuse scoring at triage with CE 3.0-shaped features + reason codes | `app/decisioning/features`, `app/decisioning/scoring` |
| Auto-adjudication under **policy-as-code** with champion/challenger and engine invariants outside the packs (no auto-denial, ever) | `policies/adjudication_v1.yaml`, `app/decisioning/policy` |
| **Agentic evidence gathering** with citations, CE 3.0 qualification, and human checkpoints before any denial | `app/decisioning/evidence` |
| Immutable, SHA-256 **hash-chained decision ledger**; case timeline reconstructable from the ledger alone | `app/decisioning/ledger` |
| **Learning loop**: closed cases retrain the model; PSI drift monitoring; live AUC & calibration | `app/decisioning/learning` |
| Pre-dispute **deflection** via Order Insight at the point of inquiry | `DisputeEngine.process_arrival`, `app/network/adapters.py` |
| Operations portal with the Phase-1 KPI baseline | `app/portal/static`, `/api/kpis` |

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi 'uvicorn[standard]' pytest httpx pyyaml
.venv/bin/python -m uvicorn app.api.server:app --port 8642
# open http://127.0.0.1:8642
```

Startup trains the misuse model on a bootstrap corpus and runs an 80-virtual-day,
~900-claim simulation (seeded; `SIM_SEED`, `SIM_CLAIMS`, `SIM_CUSTOMERS`, `SIM_DAYS`
env vars override). Everything is deterministic: the same seed reproduces the same
ledger head hash.

Tests: `.venv/bin/python -m pytest tests/` (33 tests: clock boundary math, policy
ladder, ledger tamper detection, anti-leakage, layer-boundary imports, end-to-end
invariants, deterministic replay, API).

## Portal

- **Dashboard** — STP rate, net loss & recovery, representment win rate, PC
  on-time, regulatory vs network breaches, deflection, weekly trends,
  champion-vs-challenger comparison.
- **Clock risk** — countdown board over machine-enforced deadlines with at-risk /
  breached states and automatic escalation.
- **Queues & cases** — depth, aging, drill-down to any case.
- **Case detail** — clocks with rule citations, score reason codes, evidence bundle,
  drafted investigator narrative, decisions with policy/model versions, and the
  ledger timeline (the examiner view). Pending-review cases can be approved/denied
  live from the portal through the same adjudicator path.
- **Policy, experiments & model** — the arbitration ladder as data, cohort
  performance, score distribution, calibration, PSI drift status, retrain-from-
  outcomes, and full ledger chain verification.

## Honesty notes (paper §10 preserved)

Synthetic data is generated with overlapping class distributions, label noise, and
strict as-of-filing feature extraction, so model quality is realistic rather than
flattering. Simulated outcomes depend on the decision taken, so cohort deltas carry
real signal. Nothing here claims to describe any institution's production systems
or real-world rates.

## Docs

- `docs/PLAN.md` — the build contract (v2, post-critique).
- `docs/CRITIQUE.md` — 18 adversarial review findings and their dispositions.
- `docs/ARCHITECTURE.md` — layers, flows, invariants, sourcing decisions.
