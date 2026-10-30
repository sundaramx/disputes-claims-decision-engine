// Operations portal SPA — no build step, no external deps, hand-rolled SVG charts.
const $ = (sel, el = document) => el.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtMoney = (v) => v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
const fmtPct = (v) => v == null ? "—" : v.toFixed(1) + "%";
const fmtNum = (v) => v == null ? "—" : Number(v).toLocaleString("en-US");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let KPIS = null;

// ---------------- tooltip ----------------
const tip = $("#tooltip");
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const pad = 12, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.style.display = "none"; };

// ---------------- svg helpers ----------------
const NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}
function niceTicks(max, n = 4) {
  if (max <= 0) return [0, 1];
  const step = Math.pow(10, Math.floor(Math.log10(max / n)));
  const err = max / n / step;
  const mult = err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
  const s = mult * step, ticks = [];
  for (let v = 0; v <= max + 1e-9; v += s) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + s);
  return ticks;
}
const tickLabel = (t) => t >= 1000 ? (t / 1000) + "k"
  : Number.isInteger(t) ? String(t) : String(+t.toFixed(2));
function roundedTopBar(x, y, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}

// Column chart: data = [{label, value, hint}], one series (no legend needed).
function columnChart(parent, data, { color = "var(--s1)", height = 180, fmt = fmtNum, labelEvery = 1 } = {}) {
  const W = Math.max(320, parent.clientWidth - 8), H = height;
  const m = { t: 10, r: 6, b: 22, l: 40 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H }, parent);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = Math.max(...data.map(d => d.value), 1);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1];
  for (const t of ticks) {
    const y = m.t + ih - (t / top) * ih;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: "var(--grid)", "stroke-width": 1 }, svg);
    const lbl = svgEl("text", { x: m.l - 6, y: y + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 10.5 }, svg);
    lbl.textContent = tickLabel(t);
  }
  svgEl("line", { x1: m.l, x2: W - m.r, y1: m.t + ih, y2: m.t + ih, stroke: "var(--baseline)", "stroke-width": 1 }, svg);
  const band = iw / data.length, bw = Math.min(24, band * 0.6);
  data.forEach((d, i) => {
    const x = m.l + band * i + (band - bw) / 2;
    const h = (d.value / top) * ih, y = m.t + ih - h;
    const p = svgEl("path", { d: roundedTopBar(x, y, bw, h, 4), fill: color }, svg);
    const hit = svgEl("rect", { x: m.l + band * i, y: m.t, width: band, height: ih, fill: "transparent" }, svg);
    for (const el of [p, hit]) {
      el.addEventListener("mousemove", (e) => showTip(e, `<b>${esc(d.label)}</b><br>${esc(d.hint ?? fmt(d.value))}`));
      el.addEventListener("mouseleave", hideTip);
    }
    if (i % labelEvery === 0) {
      const t = svgEl("text", { x: m.l + band * i + band / 2, y: H - 6, "text-anchor": "middle", fill: "var(--muted)", "font-size": 10.5 }, svg);
      t.textContent = d.label;
    }
  });
  // selective direct label: the extreme only
  const maxI = data.findIndex(d => d.value === Math.max(...data.map(x => x.value)));
  if (maxI >= 0 && data[maxI].value > 0) {
    const x = m.l + band * maxI + band / 2;
    const y = m.t + ih - (data[maxI].value / top) * ih - 5;
    const t = svgEl("text", { x, y, "text-anchor": "middle", fill: "var(--ink-2)", "font-size": 11, "font-weight": 600 }, svg);
    t.textContent = fmt(data[maxI].value);
  }
}

// Line chart: series = [{name, color, values}], shared xLabels. Legend for >=2.
function lineChart(parent, xLabels, series, { height = 190, fmt = fmtNum } = {}) {
  if (series.length >= 2) {
    const lg = document.createElement("div");
    lg.className = "legend";
    lg.innerHTML = series.map(s => `<span class="key"><span class="sw" style="background:${s.color}"></span>${esc(s.name)}</span>`).join("");
    parent.appendChild(lg);
  }
  const W = Math.max(320, parent.clientWidth - 8), H = height;
  const m = { t: 12, r: 14, b: 22, l: 40 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H }, parent);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = Math.max(...series.flatMap(s => s.values), 1);
  const ticks = niceTicks(max), top = ticks[ticks.length - 1];
  for (const t of ticks) {
    const y = m.t + ih - (t / top) * ih;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: "var(--grid)", "stroke-width": 1 }, svg);
    const lb = svgEl("text", { x: m.l - 6, y: y + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 10.5 }, svg);
    lb.textContent = tickLabel(t);
  }
  const xAt = (i) => m.l + (xLabels.length === 1 ? iw / 2 : (i / (xLabels.length - 1)) * iw);
  const yAt = (v) => m.t + ih - (v / top) * ih;
  xLabels.forEach((l, i) => {
    if (i % Math.ceil(xLabels.length / 10) === 0) {
      const t = svgEl("text", { x: xAt(i), y: H - 6, "text-anchor": "middle", fill: "var(--muted)", "font-size": 10.5 }, svg);
      t.textContent = l;
    }
  });
  for (const s of series) {
    const d = s.values.map((v, i) => `${i ? "L" : "M"}${xAt(i)},${yAt(v)}`).join(" ");
    svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    const last = s.values.length - 1;
    svgEl("circle", { cx: xAt(last), cy: yAt(s.values[last]), r: 4.5, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2 }, svg);
  }
  const cross = svgEl("line", { y1: m.t, y2: m.t + ih, stroke: "var(--baseline)", "stroke-width": 1, opacity: 0 }, svg);
  const hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
  hit.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = (e.clientX - r.left) * (W / r.width);
    const i = Math.max(0, Math.min(xLabels.length - 1, Math.round((px - m.l) / (iw / Math.max(xLabels.length - 1, 1)))));
    cross.setAttribute("x1", xAt(i)); cross.setAttribute("x2", xAt(i)); cross.setAttribute("opacity", 0.7);
    showTip(e, `<b>${esc(xLabels[i])}</b><br>` + series.map(s => `<span style="color:${s.color}">●</span> ${esc(s.name)}: ${fmt(s.values[i])}`).join("<br>"));
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
}

// ---------------- shared bits ----------------
const tile = (lbl, val, note = "", cls = "") => {
  const small = typeof val === "string" && /[a-z@]/i.test(val) && val.length > 6;
  return `<div class="tile ${cls}"><div class="lbl">${lbl}</div>` +
    `<div class="val" ${small ? 'style="font-size:17px;word-break:break-all;padding-top:6px"' : ""}>${val}</div>` +
    `${note ? `<div class="note">${note}</div>` : ""}</div>`;
};
const statusTile = (lbl, val, kind, icon, note = "") =>
  `<div class="tile"><div class="lbl">${lbl}</div><div class="val" >${val}</div>
   <div class="status" style="color:var(--${kind})">${icon} <span style="color:var(--ink-2)">${note}</span></div></div>`;
const clockPill = (bc) => {
  if (!bc) return `<span class="pill">no active clock</span>`;
  const days = bc.days_remaining;
  const cls = bc.state === "BREACHED" ? "crit" : bc.state === "AT_RISK" ? "warn" : days < 5 ? "warn" : "good";
  const txt = bc.state === "BREACHED" ? `⛔ BREACHED ${esc(bc.clock)}` :
    `${bc.state === "AT_RISK" ? "⚠︎ " : ""}${esc(bc.clock)} · ${days.toFixed(1)}d left`;
  return `<span class="pill ${cls}">${txt}</span>`;
};
function caseTable(cases, cols = ["id", "regime", "tier", "score", "amount", "queue", "clock", "status"]) {
  if (!cases.length) return `<div class="empty">No cases match.</div>`;
  const rows = cases.map(c => `
    <tr class="click" data-case="${c.id}">
      <td class="mono">${c.id}</td>
      <td>${c.regime}${c.escalated ? ' <span class="pill crit">ESC</span>' : ""}</td>
      <td>${c.tier ? c.tier.replace(/_/g, " ") : "—"}</td>
      <td class="num">${c.misuse_score ?? "—"}</td>
      <td class="num">${fmtMoney(c.amount)}</td>
      <td>${esc(c.queue)}</td>
      <td>${clockPill(c.binding_clock)}</td>
      <td><span class="pill ${c.status === "PENDING_REVIEW" ? "blue" : ""}">${c.status.replace(/_/g, " ")}</span></td>
    </tr>`).join("");
  return `<table><thead><tr><th>Case</th><th>Regime</th><th>Tier</th><th class="num">Misuse score</th>
    <th class="num">Amount</th><th>Queue</th><th>Binding clock</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function wireCaseRows(root) {
  root.querySelectorAll("tr[data-case]").forEach(tr =>
    tr.addEventListener("click", () => openDetail(tr.dataset.case)));
}

// ---------------- dashboard ----------------
function renderDash(k) {
  const el = $("#tab-dash");
  const c = k.compliance, e = k.efficiency, l = k.loss_recovery, cu = k.customer, m = k.model;
  const breachKind = c.regulatory_breaches === 0 ? "good" : "critical";
  const riskKind = c.at_risk_now === 0 ? "good" : "warn";
  el.innerHTML = `
    <div class="tiles">
      ${tile("Straight-through rate", fmtPct(e.stp_rate), "closed with zero human touches", "hero")}
      ${tile("Net loss", fmtMoney(l.net_loss), `recovered ${fmtMoney(l.recovered)} (${fmtPct(l.recovery_rate)})`)}
      ${tile("Representment win rate", fmtPct(l.representment_win_rate), `${fmtNum(l.representments_filed)} filed · ${fmtNum(l.representments_pending)} pending`)}
      ${tile("Provisional credit on time", fmtPct(c.pc_on_time_rate), `${fmtNum(c.pc_posted)} posted · interest ${fmtMoney(c.pc_interest_paid)}`)}
      ${statusTile("Regulatory clock breaches", fmtNum(c.regulatory_breaches), breachKind, c.regulatory_breaches === 0 ? "✓" : "⛔", `${fmtNum(c.network_breaches)} network-window misses`)}
      ${statusTile("Cases at risk now", fmtNum(c.at_risk_now), riskKind, c.at_risk_now === 0 ? "✓" : "⚠︎", "≤2 business days to a deadline")}
      ${tile("Median cycle time", (e.median_cycle_days ?? "—") + "d", `p90 ${e.p90_cycle_days ?? "—"}d`)}
      ${tile("Deflection rate", fmtPct(e.deflection_rate), `${fmtNum(e.deflected)} of ${fmtNum(e.inquiries)} inquiries`)}
      ${tile("Reopen rate", fmtPct(cu.reopen_rate), "denial overturns included")}
      ${tile("First-party flagged", fmtPct(l.first_party_flagged_rate), `denial rate ${fmtPct(l.denial_rate)}`)}
    </div>
    <div class="grid2">
      <div class="card"><h3>Weekly claim flow</h3><div class="sub">filed vs closed, by week of intake</div><div id="ch-flow"></div></div>
      <div class="card"><h3>Straight-through rate by week</h3><div class="sub">share of filed claims resolved with zero touches</div><div id="ch-stp"></div></div>
      <div class="card"><h3>Net loss by week</h3><div class="sub">after recoveries, week of intake</div><div id="ch-loss"></div></div>
      <div class="card"><h3>Tier mix</h3><div class="sub">arbitration-ladder assignment across all claims</div><div id="ch-tier"></div></div>
      <div class="card"><h3>Channel mix</h3><div class="sub">claims by payment rail (Reg E: debit/Zelle/ACH · Reg Z: credit)</div><div id="ch-chan"></div></div>
      <div class="card"><h3>Champion vs challenger</h3><div class="sub">policy experiment cohorts (engine invariants apply to both)</div><div id="cohort-tbl"></div></div>
    </div>`;
  const wk = Object.keys(k.trend);
  lineChart($("#ch-flow"), wk, [
    { name: "filed", color: css("--s1") || "var(--s1)", values: wk.map(w => k.trend[w].filed) },
    { name: "closed", color: css("--s2") || "var(--s2)", values: wk.map(w => k.trend[w].closed) },
  ]);
  lineChart($("#ch-stp"), wk, [{ name: "STP %", color: css("--s1"), values: wk.map(w => k.trend[w].stp_rate ?? 0) }], { fmt: fmtPct });
  columnChart($("#ch-loss"), wk.map(w => ({ label: w, value: k.trend[w].net_loss, hint: fmtMoney(k.trend[w].net_loss) })), { fmt: fmtMoney });
  columnChart($("#ch-tier"), Object.entries(k.portfolio.by_tier).sort().map(([t, v]) => ({ label: t.split("_")[0], value: v, hint: `${t}: ${v}` })));
  columnChart($("#ch-chan"), Object.entries(k.portfolio.by_channel).map(([t, v]) => ({ label: t.replace("CARD_", ""), value: v, hint: `${t}: ${v}` })));
  const co = k.experiments.cohorts;
  $("#cohort-tbl").innerHTML = `<table><thead><tr><th></th>
    <th class="num">Cases</th><th class="num">STP</th><th class="num">Avg touches</th>
    <th class="num">Loss bps</th><th class="num">Denial</th><th class="num">Reopen</th></tr></thead><tbody>
    ${Object.entries(co).map(([name, v]) => `<tr><td><b>${name}</b></td>
      <td class="num">${fmtNum(v.cases)}</td><td class="num">${fmtPct(v.stp_rate)}</td>
      <td class="num">${v.avg_touches ?? "—"}</td><td class="num">${fmtNum(v.loss_rate_bps)}</td>
      <td class="num">${fmtPct(v.denial_rate)}</td><td class="num">${fmtPct(v.reopen_rate)}</td></tr>`).join("")}
  </tbody></table>`;
}

// ---------------- clock risk ----------------
async function renderRisk() {
  const el = $("#tab-risk");
  const data = await (await fetch("/api/cases?open_only=true&limit=500")).json();
  const cases = data.cases;
  const breached = cases.filter(c => c.binding_clock?.state === "BREACHED");
  const atRisk = cases.filter(c => c.binding_clock?.state === "AT_RISK");
  const soon = cases.filter(c => c.binding_clock && c.binding_clock.state === "RUNNING" && c.binding_clock.days_remaining <= 7);
  el.innerHTML = `
    <div class="tiles">
      ${statusTile("Breached (open)", breached.length, breached.length ? "critical" : "good", breached.length ? "⛔" : "✓", "regulatory or network clock past due")}
      ${statusTile("At risk", atRisk.length, atRisk.length ? "warn" : "good", atRisk.length ? "⚠︎" : "✓", "≤2 business days remaining")}
      ${tile("Due within 7 days", soon.length, "open cases approaching deadline")}
      ${tile("Open cases", data.count, "all queues")}
    </div>
    <div class="card" style="margin-top:14px"><h3>Countdown board</h3>
      <div class="sub">open cases ordered by time to the binding deadline — machine-enforced clocks, automatic escalation on breach</div>
      <div id="risk-tbl">${caseTable(cases.slice(0, 80))}</div></div>`;
  wireCaseRows(el);
}

// ---------------- queues ----------------
async function renderQueues() {
  const el = $("#tab-queues");
  const k = KPIS;
  const open = await (await fetch("/api/cases?open_only=true&limit=500")).json();
  const queues = Object.entries(k.efficiency.queue_depth);
  el.innerHTML = `
    <div class="grid2" style="margin-top:0">
      <div class="card"><h3>Queue depth</h3><div class="sub">open cases by queue</div><div id="ch-q"></div></div>
      <div class="card"><h3>Aging</h3><div class="sub">open cases by age band</div><div id="ch-age"></div></div>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>Open cases</h3>
      <div class="sub">queue filter: <select id="qsel"><option value="">all queues</option>
        ${queues.map(([q]) => `<option>${q}</option>`).join("")}</select></div>
      <div id="q-tbl"></div>
    </div>`;
  columnChart($("#ch-q"), queues.map(([q, v]) => ({ label: q.replace("INVESTIGATION_", "INV "), value: v })), { color: "var(--s1)" });
  columnChart($("#ch-age"), Object.entries(k.efficiency.aging).map(([b, v]) => ({ label: b, value: v })), { color: "var(--s2)" });
  const draw = () => {
    const q = $("#qsel").value;
    const rows = open.cases.filter(c => !q || c.queue === q);
    $("#q-tbl").innerHTML = caseTable(rows.slice(0, 100));
    wireCaseRows($("#q-tbl"));
  };
  $("#qsel").addEventListener("change", draw);
  draw();
}

// ---------------- governance ----------------
async function renderGov() {
  const el = $("#tab-gov");
  const k = KPIS, m = k.model;
  const pack = k.experiments.policy_pack;
  const driftKind = m.drift_status === "ALERT" ? "critical" : m.drift_status === "WATCH" ? "warn" : "good";
  el.innerHTML = `
    <div class="tiles">
      ${tile("Policy pack", `${pack.id}@${pack.version}`.length > 18 ? pack.version : `${pack.id}@${pack.version}`, `challenger share ${pack.challenger_share}%`)}
      ${tile("Model", m.version.replace("misuse-lr-", "lr-"), `trained on ${fmtNum(m.trained_on)} labeled cases`)}
      ${tile("Training AUC", m.training_auc, "bootstrap corpus, label noise 6%")}
      ${tile("Live AUC", m.live_auc_on_closed_cases ?? "—", "on closed-case outcome labels")}
      ${statusTile("Drift (PSI, recent vs training)", m.psi_late_vs_training, driftKind,
        m.drift_status === "STABLE" ? "✓" : "⚠︎", m.drift_status + " · bands: <0.05 / <0.10 / ≥0.10")}
    </div>
    <div class="grid2">
      <div class="card"><h3>Score distribution</h3><div class="sub">live misuse scores, decile buckets</div><div id="ch-hist"></div></div>
      <div class="card"><h3>Calibration</h3><div class="sub">predicted vs actual first-party rate by score bin</div><div id="ch-cal"></div></div>
      <div class="card" style="grid-column:1/-1"><h3>Arbitration ladder — tiers as reviewable objects</h3>
        <div class="sub">score is an input; these rules decide. Engine invariants (no auto-denial, PC by deadline, Reg Z pendency) bind both cohorts.</div>
        <div id="pack-tbl"></div></div>
      <div class="card"><h3>Learning loop</h3>
        <div class="sub">closed cases are labels; candidate model challenges the incumbent</div>
        <div class="actions"><button class="primary" id="retrainBtn">Retrain from outcomes</button><span class="flash" id="retrainOut"></span></div>
        <div id="retrainDetail"></div></div>
      <div class="card"><h3>Decision ledger</h3>
        <div class="sub">append-only, SHA-256 hash-chained; every automated and human action</div>
        <div class="actions"><button class="primary" id="verifyBtn">Verify full chain</button></div>
        <div id="verifyOut"></div></div>
    </div>`;
  columnChart($("#ch-hist"), m.score_histogram.map((v, i) => ({ label: (i / 10).toFixed(1), value: v, hint: `score ${(i / 10).toFixed(1)}–${((i + 1) / 10).toFixed(1)}: ${v} cases` })), { labelEvery: 2 });
  const cal = m.calibration;
  lineChart($("#ch-cal"), cal.map(b => b.bin), [
    { name: "predicted", color: css("--s1"), values: cal.map(b => b.predicted) },
    { name: "actual", color: css("--s2"), values: cal.map(b => b.actual) },
  ], { fmt: (v) => (v * 100).toFixed(0) + "%" });
  $("#pack-tbl").innerHTML = Object.entries(pack.cohorts).map(([name, tiers]) => `
    <h3 style="margin-top:10px">${name}</h3>
    <table><thead><tr><th>Tier</th><th>Description</th><th class="num">Max score</th>
      <th class="num">Max amount</th><th>Consumer resolution</th><th>Recovery</th></tr></thead><tbody>
      ${tiers.map(t => `<tr><td><b>${t.id}</b></td><td>${esc(t.description)}</td>
        <td class="num">${t.max_misuse_score}</td><td class="num">${t.max_amount ?? "—"}</td>
        <td>${t.resolve_consumer}</td><td>${t.recovery}</td></tr>`).join("")}
    </tbody></table>`).join("");
  $("#retrainBtn").addEventListener("click", async () => {
    $("#retrainOut").textContent = "training…";
    const r = await (await fetch("/api/retrain", { method: "POST" })).json();
    $("#retrainOut").textContent = r.retrained ? "done" : r.reason;
    if (r.retrained) $("#retrainDetail").innerHTML = `<table><tbody>
      <tr><td>Incumbent</td><td class="mono">${r.incumbent_version}</td><td class="num">AUC ${r.incumbent_auc}</td></tr>
      <tr><td>Candidate</td><td class="mono">${r.candidate_version}</td><td class="num">AUC ${r.candidate_auc}</td></tr>
      <tr><td>Labeled cases</td><td class="num">${fmtNum(r.labeled_cases)}</td><td><b>${esc(r.recommendation)}</b></td></tr>
    </tbody></table>`;
  });
  $("#verifyBtn").addEventListener("click", async () => {
    const r = await (await fetch("/api/ledger/verify")).json();
    $("#verifyOut").innerHTML = r.valid
      ? `<span class="pill good">✓ chain valid — ${fmtNum(r.entries)} entries</span> <div class="mono" style="margin-top:6px">head ${r.head_hash.slice(0, 32)}…</div>`
      : `<span class="pill crit">⛔ broken at seq ${r.broken_at_seq}</span>`;
  });
}

// ---------------- case detail ----------------
async function openDetail(caseId) {
  const d = await (await fetch(`/api/cases/${caseId}`)).json();
  const s = d.summary;
  document.querySelectorAll("main > section").forEach(sec => sec.hidden = true);
  const el = $("#detail");
  el.hidden = false;
  const narrEv = d.ledger.filter(e => e.event_type === "EVIDENCE_BUNDLE").pop();
  const pc = d.provisional_credit, pz = d.reg_z_pendency;
  el.innerHTML = `
    <div class="backlink" id="backBtn">← back</div>
    <h2 style="margin:8px 0 2px">${s.id} <span class="pill ${s.status === "PENDING_REVIEW" ? "blue" : ""}">${s.status.replace(/_/g, " ")}</span>
      ${s.escalated ? '<span class="pill crit">ESCALATED</span>' : ""}</h2>
    <div class="sub" style="color:var(--ink-2)">${s.regime} · ${s.tier ?? ""} · cohort ${s.cohort ?? "—"} ·
      ${esc(d.claim.reason)} · ${fmtMoney(s.amount)} at ${esc(d.transaction.merchant_name)} · filed ${d.claim.filed_at.slice(0, 10)}</div>
    ${s.status === "PENDING_REVIEW" ? `
      <div class="actions">
        <button class="primary" id="btnPay">Approve pay (consumer favor)</button>
        <button class="primary danger" id="btnDeny">Approve denial (documented evidence)</button>
        <span class="flash" id="actOut"></span>
      </div>` : ""}
    <div class="grid2">
      <div class="card"><h3>Regulatory clocks</h3><div class="sub">machine-enforced from intake — rule basis shown per clock</div>
        <table><thead><tr><th>Clock</th><th>Due</th><th>State</th><th>Basis</th></tr></thead><tbody>
        ${d.clocks.map(c => `<tr><td><b>${c.clock_type}</b></td><td class="mono">${c.due_at.slice(0, 10)}</td>
          <td><span class="pill ${c.state === "BREACHED" ? "crit" : c.state === "AT_RISK" ? "warn" : c.state === "SATISFIED" ? "good" : ""}">${c.state}</span></td>
          <td style="max-width:340px">${esc(c.basis)}</td></tr>`).join("")}</tbody></table>
        ${d.summary.regime === "REG_E" ? `<p class="sub" style="margin-top:8px">Provisional credit: ${pc.posted_at ? `posted ${pc.posted_at.slice(0, 10)} · ${fmtMoney(pc.amount)} · notice ${pc.notice_sent_at ? "sent" : "—"} · interest ${fmtMoney(pc.interest_accrued)}` : "not posted"}${pc.reversal_notice_at ? ` · reversal noticed ${pc.reversal_notice_at.slice(0, 10)} (5bd grace)` : ""}</p>`
        : `<p class="sub" style="margin-top:8px">Reg Z pendency: ${pz.activated_at ? `active since ${pz.activated_at.slice(0, 10)} — payment hold · finance-charge freeze · adverse-reporting block · ack sent` : "not active"}</p>`}
      </div>
      <div class="card"><h3>Misuse score &amp; reason codes</h3>
        <div class="sub">model output is an input to policy, never a decision</div>
        <div class="val" style="font-size:30px;font-weight:650">${s.misuse_score ?? "—"}</div>
        <table><thead><tr><th>Feature</th><th class="num">Value</th><th class="num">Contribution</th><th>Direction</th></tr></thead><tbody>
        ${(d.score_reasons || []).map(r => `<tr><td>${esc(r.feature)}</td><td class="num">${r.value}</td>
          <td class="num">${r.contribution}</td><td>${r.direction === "raises_misuse_risk" ? "▲ raises" : "▼ lowers"}</td></tr>`).join("")}</tbody></table></div>
      <div class="card"><h3>Evidence bundle</h3><div class="sub">gathered by the evidence agent, cited in the narrative</div>
        ${d.evidence.length ? `<table><thead><tr><th>Source</th><th>Summary</th></tr></thead><tbody>
          ${d.evidence.map(e => `<tr><td><b>${e.source}</b></td><td>${esc(e.summary)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">No evidence gathered (straight-through or standard path).</div>`}
        ${narrEv ? `<h3 style="margin-top:12px">Investigator narrative (draft)</h3><pre class="narr">${esc(narrEv.payload.narrative_draft || "")}</pre>` : ""}
      </div>
      <div class="card" style="grid-column:1/-1"><h3>Decisions</h3><div class="sub">every action carries policy + model versions</div>
        <table><thead><tr><th>Stage</th><th>Action</th><th>Actor</th><th>Rule</th><th>Rationale</th></tr></thead><tbody>
        ${d.decisions.map(x => `<tr><td>${x.stage}</td><td><b>${x.action}</b></td><td>${x.actor}</td>
          <td class="mono">${esc(x.rule_id || "")}</td><td style="max-width:320px">${esc(x.rationale)}</td></tr>`).join("")}</tbody></table>
        ${d.outcome ? `<p class="sub" style="margin-top:8px"><b>Outcome:</b> ${d.outcome.resolution} · liability ${d.outcome.liability} · loss ${fmtMoney(d.outcome.loss_amount)} · recovered ${fmtMoney(d.outcome.recovered_amount)}${d.outcome.reopened ? " · REOPENED" : ""}${d.outcome.recovery_forfeited_late_filing ? " · recovery forfeited (late filing)" : ""}</p>` : ""}
      </div>
      <div class="card" style="grid-column:1/-1"><h3>Ledger timeline — the examiner view</h3>
        <div class="sub">rendered from the hash-chained ledger alone; nothing on this timeline exists outside it</div>
        <ul class="timeline">
        ${d.ledger.map(e => `<li><div class="ts">${e.ts.replace("T", " ").slice(0, 16)} · seq ${e.seq} · <span class="mono">${e.hash}</span></div>
          <b>${e.event_type}</b> ${e.payload.rationale ? "— " + esc(e.payload.rationale) : e.payload.rule_id ? "— " + esc(e.payload.rule_id) : ""}</li>`).join("")}
        </ul></div>
    </div>`;
  $("#backBtn").addEventListener("click", () => {
    el.hidden = true;
    switchTab(currentTab);
  });
  const act = async (action) => {
    $("#actOut").textContent = "applying…";
    const r = await fetch(`/api/cases/${caseId}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, rationale: action === "approve_denial" ? "Denial approved from portal on documented evidence." : "Resolved in consumer favor from portal.", reviewer: "portal.user" }),
    });
    if (r.ok) { KPIS = await (await fetch("/api/kpis")).json(); openDetail(caseId); }
    else $("#actOut").textContent = (await r.json()).detail;
  };
  $("#btnPay")?.addEventListener("click", () => act("approve_pay"));
  $("#btnDeny")?.addEventListener("click", () => act("approve_denial"));
}

// ---------------- shell ----------------
let currentTab = "dash";
function switchTab(name) {
  currentTab = name;
  $("#detail").hidden = true;
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main > section").forEach(sec => sec.hidden = sec.id !== `tab-${name}`);
  if (name === "dash") renderDash(KPIS);
  if (name === "risk") renderRisk();
  if (name === "queues") renderQueues();
  if (name === "gov") renderGov();
}
$("#tabs").addEventListener("click", (e) => {
  if (e.target.dataset.tab) switchTab(e.target.dataset.tab);
});
$("#themeBtn").addEventListener("click", () => {
  const r = document.documentElement;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = r.dataset.theme || (dark ? "dark" : "light");
  r.dataset.theme = cur === "dark" ? "light" : "dark";
  switchTab(currentTab);   // re-read CSS vars into SVG
});

async function refreshHeader() {
  KPIS = await (await fetch("/api/kpis")).json();
  $("#asof").textContent = `virtual clock: ${KPIS.as_of.slice(0, 10)} · ${fmtNum(KPIS.portfolio.total_claims)} claims · ${fmtNum(KPIS.portfolio.open_cases)} open`;
  const lv = await (await fetch("/api/ledger/verify")).json();
  $("#ledgerChip").innerHTML = lv.valid
    ? `<span class="dot" style="background:var(--good)"></span> ledger verified · ${fmtNum(lv.entries)} entries`
    : `<span class="dot" style="background:var(--critical)"></span> LEDGER BROKEN @ ${lv.broken_at_seq}`;
}

// ---------------- live simulation ----------------
let SIM = null;
let lastSimNow = null;
function renderSim() {
  if (!SIM || !SIM.enabled) return;
  $("#simChip").hidden = $("#simBtn").hidden = false;
  $("#simChip .dot").style.background = SIM.running ? "var(--good)" : "var(--muted)";
  $("#simText").textContent = SIM.running
    ? `live · +${SIM.days_run}d · ${Math.ceil(SIM.seconds_left)}s left`
    : `live sim stopped · +${SIM.days_run}d`;
  $("#simBtn").textContent = SIM.running ? "Pause" : `Resume ${SIM.run_seconds}s`;
}
async function pollSim() {
  SIM = await (await fetch("/api/sim")).json();
  renderSim();
  if (SIM.virtual_now !== lastSimNow) {
    lastSimNow = SIM.virtual_now;
    await refreshHeader();
    // live views follow the clock; governance and an open case detail are left alone
    if ($("#detail").hidden && currentTab !== "gov") switchTab(currentTab);
  }
}
$("#simBtn").addEventListener("click", async () => {
  SIM = await (await fetch(`/api/sim/${SIM.running ? "pause" : "resume"}`, { method: "POST" })).json();
  renderSim();
});
setInterval(() => pollSim().catch(() => {}), 2000);

async function boot() {
  const forced = new URLSearchParams(location.search).get("theme");
  if (forced === "light" || forced === "dark") document.documentElement.dataset.theme = forced;
  SIM = await (await fetch("/api/sim")).json();
  lastSimNow = SIM.virtual_now;
  renderSim();
  await refreshHeader();
  const hash = location.hash.slice(1);
  if (hash.startsWith("case/")) { switchTab("dash"); openDetail(hash.slice(5)); return; }
  switchTab(["dash", "risk", "queues", "gov"].includes(hash) ? hash : "dash");
}
boot();
