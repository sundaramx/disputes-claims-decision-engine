# Plan Critique & Dispositions

Independent critique of PLAN.md v1 (adversarial review agent, 2026-08-01), with dispositions.
All 18 findings **accepted**; PLAN.md v2 incorporates them. This file is the audit trail.

| # | Sev | Finding (condensed) | Disposition |
|---|-----|---------------------|-------------|
| 1 | HIGH | Reg Z cases were getting provisional credit, which exists only under Reg E. Reg Z pendency protections (no payment due on disputed amount, finance-charge freeze, no adverse credit reporting) were absent. | **Fixed.** PC guarantee and tier T1 are Reg E-only. New `RegZPendency` state on cases (finance-charge freeze + adverse-reporting block) with ledger events and a pendency-protection KPI. |
| 2 | HIGH | 45/90-day extended window encoded as unconditional; under 12 CFR 1005.11(c) it is available only if PC posted within 10/20 bd. Otherwise the 10/20 bd clock IS the resolve deadline. | **Fixed.** Extended deadline is conditional on timely PC. Unit test covers the no-PC path. |
| 3 | HIGH | PC auto-post lived in the chassis clock engine, bypassing policy/ledger — violating the paper's own separation and audit requirements. | **Fixed.** Clock lapse emits a `CaseEvent`; the adjudicator returns `POST_PROVISIONAL_CREDIT` under a named engine-invariant rule; chassis executes the accounting event. Test: no accounting event without a matching ledger entry. |
| 4 | HIGH | No virtual time — real clocks span 10bd–90d, so a wall-clock demo produces empty compliance KPIs. Highest-risk build item is the virtual-time lifecycle driver. | **Fixed.** Injectable `SimClock` drives chassis, clock engine, ledger timestamps. Simulator advances day-by-day. Lifecycle driver moved to build step 3. |
| 5 | HIGH | Outcomes generated from truth labels alone make champion/challenger and the learning loop zero-signal theater. | **Fixed.** Outcome model = f(true type, decision taken, evidence quality), documented in simulation module. |
| 6 | HIGH | Feature/label leakage (post-claim rows; cleanly separated distributions) would yield a misleadingly perfect model. | **Fixed.** As-of-filed feature extraction with a leakage unit test; overlapping class-conditional distributions + label noise; honest AUC/calibration shown on the portal. |
| 7 | MED | Consumer-favor + clock invariants not guaranteed for challenger cohorts. | **Fixed.** Invariants (no auto-denial, PC-by-deadline, notice timing) enforced by the engine outside policy packs, all cohorts. Property test runs both cohorts. |
| 8 | MED | Missing issuer chargeback-filing window (~120d) clock and "recovery forfeited by late filing" KPI. | **Fixed.** `NETWORK_FILING_WINDOW` clock + late-filing forfeiture in outcomes + KPI. |
| 9 | MED | VCR modeled undifferentiated; CE 3.0 applies only to CNP fraud (10.4/allocation); Zelle/ACH have no chargeback rights. | **Fixed.** `NetworkCategory` on claims; CE 3.0 evaluation scoped to FRAUD_CNP; Zelle/ACH excluded from representment flows and win-rate KPI. |
| 10 | MED | Ledger not self-contained (payload elsewhere/mutable); hash recipe unspecified. | **Fixed.** Ledger row stores canonical JSON payload; `hash = SHA256(prev_hash || seq || ts || event_type || canonical_payload)`; `/api/ledger/verify` endpoint. |
| 11 | MED | Human actions, clock transitions, notices not explicitly ledgered; examiner reconstructs the whole case. | **Fixed.** Every case-affecting event ledgered regardless of actor; portal case timeline renders from the ledger alone. |
| 12 | MED | Reg Z "2 complete billing cycles ≤ 90 days" not computable without cycle dates. | **Fixed.** `cycle_close_day` added to credit accounts; deadline = min(second cycle close after notice, 90 days). |
| 13 | MED | T0 conflated consumer resolution with liability write-off, inflating loss KPIs. | **Fixed.** Tiers decouple `resolve_consumer` from `recovery_decision`; T0 writes off only below a recovery-economics threshold, else files chargeback post-payment. |
| 14 | MED | Interest on provisional credit omitted — a recurring examiner finding the paper flags twice. | **Fixed.** Interest accrual computed on PC for interest-bearing accounts; surfaced in case detail and compliance KPIs. |
| 15 | MED | Deflection KPI had no producing mechanism; VAMP 0.9% is a merchant/acquirer-side metric, not an issuer KPI. | **Fixed.** Pre-claim `inquiry → Order Insight → deflected/converted` stage added to the simulator + intake API. VAMP reframed as merchant-risk context on the portal, not an issuer KPI tile. |
| 16 | LOW | Regime routing unstated; wires undefined. | **Fixed.** Wires cut from scope (UCC 4A, documented). Total routing table: CARD_DEBIT/ZELLE/ACH → Reg E; CARD_CREDIT → Reg Z. Unit-tested. |
| 17 | LOW | Chassis/decisioning boundary asserted, not mechanized. | **Fixed.** Import-boundary test walks module imports and fails on cross-layer imports outside the narrow interface types. |
| 18 | LOW | Missing high-value tests: bd-10/20 and day-45/90 boundaries, Reg Z never gets PC, breach→escalation, drift panel fires via mid-run mix shift, deterministic replay (same seed → same ledger head hash). | **Fixed.** All added to the test plan; simulator includes a scheduled mid-run first-party-misuse mix shift so PSI drift demonstrably fires. |

**Scope cuts adopted:** wires; RDR/CDRN/Mastercom adapters (Order Insight + Consumer Clarity
kept; Mastercom documented as stub); pre-arbitration; portal screens "policy/experiments" and
"model monitor" merged into one governance screen.
