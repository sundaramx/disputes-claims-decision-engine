# Implementation Plan — Intelligent Disputes & Claims Decision Engine + Operations Portal

**Version:** 2.0 (confirmed after independent critique — see `CRITIQUE.md` for the 18
findings and dispositions; §15 lists the amendments)
**Source:** `Intelligent_Disputes_and_Claims.docx` (Sundaram Thevar, July 2026 working draft)
**Date:** 2026-08-01

---

## 1. Objective

Build a working, best-in-class **reference implementation** of the paper's target architecture:
a disputes & claims **decision engine** (the "build" layer: triage scoring, policy-as-code
auto-adjudication, regulatory clock enforcement, agentic evidence gathering, immutable decision
ledger, learning loop) plus an **operations portal** that tracks the business metrics and KPIs
the paper says Phase 1 must instrument.

The paper's core design principles become hard requirements:

1. **Generative/ML components score and draft; deterministic policy decides.** No
   customer-affecting action executes from free-form model output.
2. **Timeline compliance is a property of the system**, not analyst diligence — every case
   carries machine-enforced Reg E / Reg Z clocks from intake.
3. **Every automated action writes its rationale to an immutable, hash-chained decision
   ledger** (inputs, feature values, model version, policy version, rationale).
4. **Narrow chassis API** — the case-management chassis (workflow state, SLA clocks,
   accounting events) is separated from the decisioning layer so either side is replaceable.
5. **Auto-adjudication only ever resolves in the consumer's favor automatically** (early-phase
   control from §9); denials always require human review.
6. **Network utilities are rented** — Verifi/Ethoca/VROL/Mastercom are integration adapters
   with mock backends, never re-implemented logic.

## 2. Scope

**In scope (working software):**
- Domain model: customers, accounts, transactions, claims, cases, evidence, outcomes.
- Regulatory clock engine: Reg E (10bd investigate / 20bd new account / provisional credit /
  45d / 90d POS-foreign-new), Reg Z (30d acknowledgment, 2 billing cycles ≤ 90d), network
  clocks (VCR 30-day merchant response window).
- First-party misuse scoring at triage (CE 3.0-shaped features: prior undisputed purchases at
  merchant, device/IP continuity, address/login history, dispute history, serial-disputer
  signals) with a trained + calibrated model and reason codes.
- Policy-as-code engine: versioned declarative policy packs (YAML), arbitration-ladder tiers,
  champion/challenger slots with deterministic assignment, full decision records.
- Agentic evidence orchestrator: tool-using investigation over mock adapters (auth logs, 3DS,
  Verifi Order Insight, Ethoca Consumer Clarity, contact history), drafted investigator
  narrative with citations, human-checkpoint gates.
- Immutable decision ledger: append-only, SHA-256 hash-chained, verifiable.
- Learning loop: closed-case outcomes recalibrate the misuse model and produce
  champion/challenger performance comparison + drift monitoring.
- Simulation harness: synthetic portfolio generator (configurable claim mix incl. true fraud,
  merchant error, first-party misuse, ACH/Zelle/card mix) that drives the engine end-to-end.
- REST API (FastAPI) exposing intake, case, decision, queue, metrics endpoints.
- Operations portal (served web UI): KPI dashboard, SLA/clock risk board, case queues, case
  detail with ledger timeline, policy & champion/challenger view, model monitoring view.
- Test suite: unit tests for clocks/policy/ledger/scoring + end-to-end scenario tests.

**Out of scope (documented as interfaces only):** real network connectivity, real core
banking, authN/authZ, PII handling, production deployment infra.

## 3. Stack

- **Backend:** Python 3.14, FastAPI + Pydantic v2, SQLite (stdlib `sqlite3`) as the case
  store/lakehouse stand-in. Pure-Python logistic model (no heavyweight ML deps) trained on the
  synthetic corpus — transparent, seedable, explainable.
- **Portal:** server-served single-page app, no build step, no CDN (works offline): vanilla
  ES modules + hand-rolled SVG charts following the dataviz design system.
- **Tests:** pytest + httpx TestClient.
- **Business-day math:** pure-Python US federal holiday calendar (2025–2027) — Reg E clocks
  are business-day based; this must be exact.

Rationale: zero-flake local runnability beats framework fashion; the architecture (narrow
interfaces, policy-as-code, ledger) is where "state of the art" lives, not in the web
framework.

## 4. Architecture (paper Figure 2 → concrete modules)

```
app/
  domain/        # entities, enums, events (payment types, claim reasons, Reg E/Z categories)
  chassis/       # case store, workflow state machine, SLA clock engine, accounting events
                 #   <-- "buy/retain" layer, kept behind a narrow interface (ChassisAPI)
  decisioning/   # <-- the "build" layer
    features/    #   feature extraction from transaction/device/identity history
    scoring/     #   first-party misuse model + calibration + reason codes
    policy/      #   policy-as-code engine, tier ladder, champion/challenger
    adjudicator/ #   orchestrates score -> policy -> decision; consumer-favor-only autos
    evidence/    #   agentic evidence orchestrator + tool adapters (mock Verifi/Ethoca/auth/3DS)
    ledger/      #   append-only hash-chained decision ledger
    learning/    #   outcome joins, recalibration, drift metrics, champ/challenger eval
  network/       # rent-layer adapters: OrderInsight, ConsumerClarity, RDR/Alerts, VROL filing
  api/           # FastAPI routers: intake, cases, queues, decisions, metrics, policies, admin
  portal/        # static SPA assets
  simulation/    # synthetic data generator + lifecycle simulator
  tests/
docs/            # PLAN.md, CRITIQUE.md, ARCHITECTURE.md, KPI_CATALOG.md
```

**Chassis ↔ decisioning interface (narrow API, per §5 of the paper):** chassis emits
`CaseEvent`s and calls `DecisionService.decide(case_snapshot) -> Decision`; decisioning never
mutates chassis state directly. This is enforced by module boundaries and tested.

## 5. Domain & data model (key tables)

- `customers(id, tenure_days, segment, prior_claims_count, ...)`
- `accounts(id, customer_id, type[debit|credit], opened_at, is_new_account)`
- `transactions(id, account_id, merchant_id, mcc, amount, channel, device_id, ip, auth_result,
  three_ds, timestamp, delivery_address_match, ...)`
- `claims(id, account_id, txn_id, reason_code, claimed_amount, channel[card|zelle|ach|wire],
  narrative, intake_channel, filed_at)`
- `cases(id, claim_id, status, reg_regime[REG_E|REG_Z|NETWORK_ONLY], clocks(json),
  provisional_credit(status, due_at, posted_at), assigned_queue, tier, cohort, ...)`
- `decisions(id, case_id, stage, action, policy_id, policy_version, model_version, score,
  reasons(json), actor[system|human], ledger_seq)`
- `ledger(seq, ts, case_id, event_type, payload_hash, prev_hash, hash)`
- `evidence(id, case_id, source, artifact(json), gathered_by, cited_in_narrative)`
- `outcomes(case_id, resolution, liability, loss_amount, recovered_amount,
  representment_result, closed_at, label_first_party)`

## 6. Regulatory clock engine (exact rules encoded)

| Clock | Trigger | Deadline | Consequence encoded |
|---|---|---|---|
| Reg E investigate | notice of error | 10 business days (20 new account) | must resolve OR post provisional credit |
| Reg E provisional credit notify | PC posted | 2 business days | notice event required |
| Reg E extended resolve | notice | 45 calendar days (90 for POS/foreign/new) — **available only if PC posted within the 10/20bd window; otherwise the 10/20bd clock IS the resolve deadline** (12 CFR 1005.11(c)) | conditional hard deadline |
| Network filing window | claim intake (card only) | ~120 calendar days from transaction | late filing forfeits recovery (KPI) |
| Reg E correction | error determined | 1 business day to correct, 3 to report | events required |
| Reg E PC reversal | reversal decision | notice + 5 business-day grace honoring debits | reversal gate |
| Reg Z acknowledgment | billing error notice | 30 days | ack letter event |
| Reg Z resolution | notice | 2 complete billing cycles, ≤ 90 days | hard deadline |
| VCR merchant response | chargeback filed | 30 days | representment window |

Clock engine emits `at_risk` (configurable threshold, default ≤2bd remaining) and `breached`
states; escalation is automatic queue promotion. Provisional credit is a **system guarantee
executed through the decisioning layer**: an at-risk investigate clock emits a `CaseEvent`;
the adjudicator returns `POST_PROVISIONAL_CREDIT` under a named engine-invariant rule; the
chassis executes and ledgers the accounting event (never a silent chassis side-effect).
**Provisional credit is Reg E-only.** Reg Z cases instead enter a pendency state: disputed
amount not payable, finance-charge freeze, adverse-credit-reporting block — tracked and
ledgered, with a pendency-protection KPI. Interest accrues on PC for interest-bearing
accounts. All clocks/timestamps run on an injectable virtual clock (`SimClock`) so the
simulation can mature 10bd–90d clocks in seconds.

## 7. Scoring & learning loop

- **Features (CE 3.0-aligned + issuer-only):** count/recency of prior undisputed txns at same
  merchant (120–365d window per CE 3.0), device continuity, IP continuity, delivery-address
  match, account tenure, prior dispute count & rate, household duplicate-claim signal,
  serial-disputer velocity, amount-to-history ratio, channel risk.
- **Model:** logistic regression trained in-repo on the synthetic labeled corpus (every closed
  case is a label — the paper's compounding data asset), with Platt-style calibration and
  per-decision reason codes (top signed feature contributions).
- **Governance:** model version pinned in every decision record; drift monitor compares
  live score distribution vs. training reference (PSI); disparate-impact hook documented.
- **Champion/challenger:** deterministic hash-based cohort assignment; policy packs declare
  champion + challenger; portal reports per-cohort outcomes.

## 8. Policy-as-code

YAML policy packs, e.g. `policies/adjudication_v1.yaml`:
- Tiers as discrete reviewable objects: `T0_auto_pay` (low value, low misuse score, clean
  history → auto-resolve in consumer favor + write-off), `T1_provisional_fast` (PC now,
  standard investigation), `T2_enhanced_investigation` (high misuse score → evidence agent +
  human), `T3_defended_denial_candidate` (human-only decision, documented evidence required).
- Guards encoded as data: max auto amount, score bands, eligibility (no auto-denial ever),
  regime-specific behavior.
- Every evaluation returns matched-rule trace for the ledger.

## 9. Agentic evidence orchestrator

Plan → act → cite loop over typed tools: `AuthLogTool`, `ThreeDSTool`, `OrderInsightTool`,
`ConsumerClarityTool`, `ContactHistoryTool`, `PriorClaimsTool`. Output: structured evidence
bundle + investigator narrative draft with per-paragraph citations + CE 3.0 qualification
assessment (does merchant evidence meet the 2-undisputed-txn 120–365d test?). Human
checkpoint: narrative and any network-facing filing enter `PENDING_REVIEW`; API + portal
expose approve/reject. No LLM dependency: the "agent" is a deterministic planner with the
same tool-use architecture, so it runs offline and is testable (swap-in point for an LLM
documented).

## 10. KPI catalog (portal scope — paper §8 Phase 1 baseline + industry standards)

**Compliance:** % PC posted within 10/20bd; % resolved within 45/90d (Reg E) and within Reg Z
windows; clock-at-risk count now; breaches (target 0); PC notice timeliness.
**Efficiency:** straight-through rate (auto-adjudicated share); touches per case; median/p90
cycle time by regime & channel; queue depth & aging; unit cost proxy.
**Loss & recovery:** gross loss rate, net loss after recovery, representment win rate,
recovery rate, write-off by tier; first-party misuse flagged/confirmed rates; deflection rate
at intake; chargeback ratio vs. VAMP 0.9% threshold context.
**Model & policy health:** score distribution + PSI drift, champion vs challenger outcome
delta, calibration curve, auto-adjudication precision (reopen rate on auto-paid).
**Customer experience:** reopen rate, time-to-provisional-credit, denial overturn rate.

## 11. Portal screens

1. **Executive dashboard** — KPI tiles + trend charts (STP, cycle time, loss, compliance).
2. **Clock risk board** — cases by time-remaining bands; breach list; auto-escalations.
3. **Queues** — triage/investigation/review queues with SLA countdowns, sortable.
4. **Case detail** — timeline, clocks, score + reasons, evidence bundle, narrative draft,
   ledger chain view, human approve/deny actions.
5. **Policy & experiments** — active policy pack, tier definitions, champion/challenger
   performance.
6. **Model monitor** — score distributions, drift, calibration, version history.

## 12. Validation plan

- Unit: business-day math (holiday cases), each clock, policy tier selection, ledger chain
  tamper detection, feature extraction, scoring reason codes, consumer-favor-only invariant.
- Property-style: simulate N cases → assert zero unhandled clock breaches, every automated
  decision has ledger entry + policy/model versions, no auto-denials.
- E2E: run simulation (≈3–5k claims, mixed types) through intake→resolution; compute KPIs;
  verify portal endpoints serve them; verify ledger chain integrity over the full run.
- Manual: launch server, exercise portal, capture evidence of working screens.

## 13. Build order

1. Scaffold + venv + deps; domain model; SQLite store.
2. Business-day calendar + clock engine (+tests).
3. Feature extraction + synthetic data generator (needed to train scorer).
4. Scorer + calibration + reason codes (+tests).
5. Policy engine + adjudicator + champion/challenger (+tests).
6. Ledger (+tamper tests).
7. Evidence orchestrator + network adapters (+tests).
8. Chassis workflow + simulation lifecycle driver.
9. Learning loop + metrics computation.
10. API routers (+tests).
11. Portal SPA.
12. E2E validation run + docs (ARCHITECTURE.md, README).

## 14. Risks

- **Scope creep** → timebox portal polish; KPIs before beauty.
- **Clock-math bugs** → exhaustive unit tests incl. holidays/weekends.
- **Synthetic data realism** → parameterized mix with documented assumptions; not a claim
  about real-world rates (paper §10 caveat honored).
- **"Agentic" overclaim** → deterministic planner clearly labeled; LLM swap-in documented.

## 15. Version 2 amendments (from independent critique — full detail in CRITIQUE.md)

1. **Reg Z corrections:** no provisional credit under Reg Z; Reg Z pendency protections
   (payment hold on disputed amount, finance-charge freeze, adverse-reporting block) added
   as first-class ledgered state with a KPI. Tier `T1_provisional_fast` is Reg E-only; Reg Z
   equivalent is `T1` with pendency activation. Reg Z resolve deadline computed from real
   billing-cycle closes (`cycle_close_day` on credit accounts): min(2nd cycle close, 90d).
2. **Conditional Reg E extension:** 45/90d window exists only if PC posted within 10/20bd.
3. **PC guarantee via decisioning layer:** clock-lapse `CaseEvent` → adjudicator decision
   under engine-invariant rule → chassis executes; ledgered like every other action.
4. **Virtual time:** injectable `SimClock` throughout; lifecycle driver built early (step 3).
5. **Decision-dependent outcomes:** simulation outcome = f(true type, decision taken,
   evidence quality) so champion/challenger deltas and the learning loop carry real signal.
6. **Anti-leakage:** as-of-filed feature extraction (unit-tested), overlapping
   class-conditional distributions, label noise; honest AUC/calibration on the portal.
7. **Engine-level invariants outside policy packs** (no auto-denial, PC-by-deadline, notice
   timing) apply to champion AND challenger cohorts; property-tested.
8. **Issuer network filing window** (~120d) clock + late-filing forfeiture KPI.
9. **Network categories:** FRAUD_CNP / FRAUD_CP / CONSUMER_DISPUTE / PROCESSING_ERROR on
   card claims; CE 3.0 evaluation scoped to FRAUD_CNP; Zelle/ACH excluded from
   representment flows and win-rate KPIs (no chargeback rights).
10. **Self-contained ledger:** canonical JSON payload stored in the ledger row;
    `hash = SHA256(prev_hash || seq || ts || event_type || canonical_payload)`;
    `/api/ledger/verify`; ALL case-affecting events ledgered (human actions, clock
    transitions, notices); portal case timeline renders from the ledger alone.
11. **Tier semantics:** consumer resolution decoupled from recovery decision; T0 auto-pay
    still files chargeback when economics justify (write-off only below threshold).
12. **Interest on PC** for interest-bearing accounts.
13. **Deflection stage:** pre-claim inquiry → Order Insight → deflected/converted in
    simulator + API; VAMP 0.9% reframed as merchant-risk context, not an issuer KPI.
14. **Scope cuts:** wires (UCC 4A, documented); RDR/CDRN/Mastercom stubs only;
    pre-arbitration; portal governance screens merged into one.
15. **Boundary enforcement test** (imports) + added tests: bd-10/20 & day-45/90 boundaries,
    Reg Z-never-gets-PC, breach→escalation, drift fires via scheduled mid-run mix shift,
    deterministic replay (same seed → same ledger head hash).

**Routing table (total):** CARD_DEBIT → Reg E · ZELLE → Reg E · ACH → Reg E ·
CARD_CREDIT → Reg Z. (Wires out of scope.)

**Plan v2 is confirmed as the build contract.**
