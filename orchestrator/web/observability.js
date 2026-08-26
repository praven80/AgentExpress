/* Observability tab — self-contained.
 *
 * Everything for the cost/latency/token views lives here: styles, data fetches
 * (the /api/telemetry/* endpoints), charts (Chart.js), and drilldown. It only
 * borrows the global `api()` and `esc()` from index.html; remove the two <script>
 * tags + this file and the app is unchanged.
 *
 * Two levels:
 *   Overview   — aggregate by date / model over a range (chart + table).
 *   Run detail — one session: per-agent cost/latency/tokens, expandable to
 *                   the individual model/tool/compute calls (lowest level).
 */
(function () {
  "use strict";

  // ---- small helpers (reuse index.html globals when present) ----------------
  const $ = (id) => document.getElementById(id);
  const esc = window.esc || ((s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m])));
  const api = (p, o) => window.api(p, o);

  const fmtUsd = (x) => "$" + Number(x || 0).toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  const fmtInt = (x) => Number(x || 0).toLocaleString();
  const fmtMs = (x) => { x = Number(x || 0); return x >= 1000 ? (x / 1000).toFixed(1) + "s" : Math.round(x) + "ms"; };
  // Eastern-Time helpers (match the rest of the project).
  const etDateStr = () => new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const etStamp = () => new Date().toLocaleString("sv-SE", { timeZone: "America/New_York" })
    .replace(" ", "_").replace(/:/g, "-");
  // Shift a YYYY-MM-DD string by N days (noon-UTC anchor avoids tz/DST edges).
  const shiftDays = (ymd, days) => { const d = new Date(ymd + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };

  // Generic disclaimer shown on every cost view.
  const LEGEND = "These cost and token figures are estimates for planning — they "
    + "will not match your actual AWS bill (private pricing, region, and caching "
    + "discounts differ).";

  // Price-book provenance — keep in sync with app/observability/pricing.py.
  const PRICES_AS_OF = "2026-08-20";
  const PRICES_REGION = "us-east-1";

  // ---- styles (kept here so the tab is fully self-contained) -----------------
  // Reuses the app's global CSS variables (--blue, --surface, --muted, …) so the
  // tab is visually consistent with the rest of the product.
  const CSS = `
  .topnav { display:inline-flex; gap:4px; margin-left:14px; }
  .navbtn { background:transparent; border:1px solid transparent; color:#cbd5e1; font-weight:600;
            font-size:13px; padding:6px 12px; border-radius:8px; cursor:pointer; transition:background .15s; }
  .navbtn:hover { background:rgba(255,255,255,.08); }
  .navbtn.active { background:#fff; color:#0f172a; box-shadow:0 1px 3px #0f172a22; }

  #obsView { flex:1; overflow-y:auto; background:linear-gradient(160deg,var(--bg1,#eef2f9),var(--bg2,#e6ebf5)); }
  .obs-inner { padding:26px 30px 60px; max-width:1240px; margin:0 auto; }

  /* Page heading */
  .obs-hero { display:flex; align-items:flex-start; gap:16px; margin-bottom:20px; }
  .obs-hero .obs-hero-ic { width:44px; height:44px; border-radius:13px; flex:0 0 auto;
      background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff; font-size:22px;
      display:flex; align-items:center; justify-content:center; box-shadow:0 6px 16px #1e3a8a44; }
  .obs-hero h1 { margin:0 0 3px; font-size:20px; font-weight:800; letter-spacing:-.02em; color:#0f172a; }
  .obs-hero p { margin:0; font-size:13px; color:var(--muted,#64748b); max-width:640px; line-height:1.5; }

  /* Trust banner */
  .obs-banner { display:flex; align-items:center; gap:13px; background:linear-gradient(180deg,#f8fafc,#eef2f7);
      border:1px solid var(--border,#e2e8f0); border-radius:14px; padding:13px 18px; margin-bottom:16px; }
  .obs-banner .ck { width:24px; height:24px; border-radius:50%; background:#64748b; color:#fff; flex:0 0 auto;
      display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:800; font-style:italic;
      font-family:Georgia,serif; box-shadow:0 2px 6px #64748b44; }
  .obs-banner .bt { font-size:12.5px; color:#334155; line-height:1.5; }
  .obs-banner .bt b { font-weight:700; color:#0f172a; }
  .obs-banner .bt .meta { color:var(--muted,#64748b); }

  /* Tabs */
  .obs-tabs { display:inline-flex; gap:4px; margin-bottom:18px; background:#fff; border:1px solid var(--border,#e2e8f0);
      border-radius:12px; padding:4px; box-shadow:0 1px 3px #0f172a0f; }
  .obs-tab { padding:8px 18px; border-radius:9px; border:0; background:transparent;
             font-weight:700; font-size:13px; cursor:pointer; color:var(--muted,#64748b); transition:all .15s; }
  .obs-tab:hover { color:#0f172a; }
  .obs-tab.active { background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff; box-shadow:0 2px 8px #2563eb44; }

  /* Cards */
  .obs-card { background:var(--surface,#fff); border:1px solid var(--border,#e2e8f0); border-radius:16px;
      padding:20px 22px; margin-bottom:18px; box-shadow:0 1px 3px #0f172a0f; }
  .obs-card > h3 { margin:0 0 15px; font-size:12px; font-weight:700; color:var(--muted2,#94a3b8);
      text-transform:uppercase; letter-spacing:.05em; display:flex; align-items:center; gap:8px; }
  .obs-card > h3 .obs-note { text-transform:none; letter-spacing:0; font-weight:500; }

  /* Controls */
  .obs-controls { display:flex; flex-wrap:wrap; gap:14px; align-items:end; }
  .obs-controls label { display:flex; flex-direction:column; gap:5px; font-size:10.5px; font-weight:700;
                        text-transform:uppercase; letter-spacing:.04em; color:var(--muted,#64748b); }
  .obs-controls input, .obs-controls select { border:1px solid var(--border2,#cbd5e1); border-radius:9px; padding:8px 11px;
                        font-size:13px; min-width:150px; background:#fff; color:#0f172a; transition:border-color .15s, box-shadow .15s; }
  .obs-controls input:focus, .obs-controls select:focus { outline:none; border-color:var(--blue,#2563eb); box-shadow:0 0 0 3px #2563eb22; }
  .obs-controls .grow { flex:1; }
  .obs-btn { background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff; border:none; border-radius:9px; padding:9px 18px;
             font-weight:700; font-size:13px; cursor:pointer; box-shadow:0 2px 6px #2563eb33; transition:filter .15s; }
  .obs-btn:hover { filter:brightness(1.06); }
  .obs-btn.ghost { background:#fff; color:var(--blue,#2563eb); border:1px solid #bfdbfe; box-shadow:none; }
  .obs-btn.ghost:hover { background:#eff6ff; filter:none; }
  .obs-exports { display:flex; gap:8px; }

  /* KPI strip */
  .obs-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin-bottom:18px; }
  .obs-kpi { position:relative; background:var(--surface,#fff); border:1px solid var(--border,#e2e8f0); border-radius:14px;
      padding:16px 18px 15px; box-shadow:0 1px 3px #0f172a0f; overflow:hidden; }
  .obs-kpi::before { content:""; position:absolute; top:0; left:0; bottom:0; width:4px; background:var(--kpi-accent,#2563eb); }
  .obs-kpi .v { font-size:24px; font-weight:800; color:#0f172a; letter-spacing:-.02em; line-height:1.1; font-variant-numeric:tabular-nums; }
  .obs-kpi .l { font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--muted,#64748b); margin-top:5px; }
  .obs-kpi .s { font-size:11px; color:var(--muted2,#94a3b8); margin-top:3px; }

  /* Charts */
  .obs-chart-wrap { position:relative; height:300px; }

  /* Tables */
  table.obs { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.obs th, table.obs td { text-align:left; padding:9px 12px; border-bottom:1px solid #eef2f7; }
  table.obs thead th { font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted,#64748b);
      font-weight:700; border-bottom:1.5px solid #e5e9f0; background:#fafbfd; position:sticky; top:0; }
  table.obs tbody tr:hover { background:#f8fafc; }
  table.obs td.num, table.obs th.num { text-align:right; font-variant-numeric:tabular-nums; }
  table.obs td.strong { font-weight:700; color:#0f172a; }
  .obs-tablewrap { max-height:520px; overflow:auto; border-radius:10px; border:1px solid #eef2f7; }
  tr.obs-agent { cursor:pointer; } tr.obs-agent:hover { background:#f4f7ff; }
  tr.obs-agent td.strong { color:#1e3a8a; }
  tr.obs-agent .caret { color:var(--blue,#2563eb); display:inline-block; width:12px; font-size:11px; }
  tr.obs-calls td { background:#fbfdff; padding:0; }
  .obs-calls-inner { padding:8px 12px 14px 30px; }

  /* Pills */
  .obs-pill { display:inline-block; font-size:10px; font-weight:700; padding:2px 8px; border-radius:999px; text-transform:capitalize; }
  .obs-pill.llm { background:#eef2ff; color:#4338ca; }
  .obs-pill.tool { background:#ecfdf5; color:#047857; }
  .obs-pill.agentcore { background:#fff7ed; color:#c2410c; }
  .obs-pill.simulated { background:#f1f5f9; color:#64748b; }

  /* Est. marker + notes */
  .obs-est { color:var(--muted2,#94a3b8); font-style:italic; }
  .obs-note { font-size:11.5px; color:var(--muted2,#94a3b8); margin-top:10px; line-height:1.55; }
  .obs-empty { color:var(--muted2,#94a3b8); padding:40px; text-align:center; font-size:13px; }

  /* Session combobox (dropdown + typeahead) */
  .obs-combo { position:relative; width:380px; max-width:100%; }
  .obs-combo > input { width:100%; padding-right:30px; }
  .obs-combo-caret { position:absolute; right:4px; top:50%; transform:translateY(-50%); background:transparent;
      border:0; color:var(--muted,#64748b); cursor:pointer; font-size:12px; line-height:1; padding:6px 8px; }
  .obs-combo-caret:hover { color:#0f172a; }
  .obs-combo.open .obs-combo-caret { transform:translateY(-50%) rotate(180deg); }
  .obs-combo-list { position:absolute; z-index:30; top:calc(100% + 4px); left:0; right:0; max-height:280px;
      overflow-y:auto; background:#fff; border:1px solid var(--border2,#cbd5e1); border-radius:11px;
      box-shadow:0 10px 28px #0f172a24; padding:4px; }
  .obs-combo-opt { padding:8px 11px; border-radius:8px; cursor:pointer; }
  .obs-combo-opt:hover, .obs-combo-opt.active { background:#eff6ff; }
  .obs-combo-opt .ot { font-size:13px; color:#0f172a; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .obs-combo-opt .oid { font-family:ui-monospace,Menlo,monospace; font-size:11px; color:var(--muted2,#94a3b8); margin-top:1px; }
  .obs-combo-empty { padding:12px; font-size:12.5px; color:var(--muted2,#94a3b8); text-align:center; }

  /* Projection */
  .obs-proj { display:flex; flex-wrap:wrap; gap:22px; align-items:center; }
  .obs-proj .pcol { display:flex; flex-direction:column; gap:3px; }
  .obs-proj .pcol .pv { font-size:22px; font-weight:800; color:#0f172a; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
  .obs-proj .pcol .pl { font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--muted,#64748b); }
  .obs-proj .psep { width:1px; align-self:stretch; background:var(--border,#e2e8f0); }
  .obs-proj .prunbox { display:flex; align-items:center; gap:8px; }
  .obs-proj input { width:80px; border:1px solid var(--border2,#cbd5e1); border-radius:8px; padding:7px 9px; font-size:14px;
      font-weight:700; text-align:center; }
  .obs-proj input:focus { outline:none; border-color:var(--blue,#2563eb); box-shadow:0 0 0 3px #2563eb22; }
  .obs-proj .pbig { font-size:26px; font-weight:800; color:#1e3a8a; letter-spacing:-.02em; }
  `;
  const style = document.createElement("style"); style.textContent = CSS; document.head.appendChild(style);

  // ---- chart lifecycle -------------------------------------------------------
  const charts = {};
  function draw(id, config) {
    if (!window.Chart) return;
    if (charts[id]) charts[id].destroy();
    charts[id] = new window.Chart($(id), config);
  }
  const PALETTE = ["#2563eb", "#7c3aed", "#059669", "#d97706", "#dc2626", "#0891b2",
                   "#db2777", "#65a30d", "#4f46e5", "#ea580c", "#0d9488", "#9333ea", "#e11d48"];

  // ---- one-time layout -------------------------------------------------------
  let built = false, tab = "overview", flowCalls = [], lastBuckets = [], lastBy = "date";
  function build() {
    if (built) return; built = true;
    const todayStr = etDateStr();                       // ET "today"
    const fromStr = shiftDays(todayStr, -29);           // 30-day default window
    $("obsView").innerHTML = `
     <div class="obs-inner">
      <div class="obs-hero">
        <div class="obs-hero-ic">📊</div>
        <div>
          <h1>Observability &amp; Cost</h1>
          <p>Every model call, tool call and compute burst captured per run — with token
             counts, latency and cost you can drill into, aggregate, and plan against.</p>
        </div>
      </div>

      <div class="obs-banner">
        <div class="ck">i</div>
        <div class="bt"><b>Costs and token counts are estimates for planning.</b>
          They will not match your actual AWS bill (private pricing, region, and caching discounts differ).
          <span class="meta">Price book: AWS list prices, ${esc(PRICES_REGION)}, as of ${esc(PRICES_AS_OF)}.</span></div>
      </div>

      <div class="obs-tabs">
        <button class="obs-tab active" id="obsTabOverview" onclick="obsGoto('overview')">Overview</button>
        <button class="obs-tab" id="obsTabFlow" onclick="obsGoto('flow')">Run detail</button>
      </div>

      <section id="obsOverview">
        <div class="obs-card">
          <div class="obs-controls">
            <label>From<input type="date" id="obsFrom" value="${fromStr}"></label>
            <label>To<input type="date" id="obsTo" value="${todayStr}"></label>
            <label>Group by
              <select id="obsBy">
                <option value="date">Date</option>
                <option value="model">Model</option>
                <option value="user">User</option>
              </select>
            </label>
            <button class="obs-btn" onclick="obsLoadOverview()">Apply</button>
            <span class="grow"></span>
            <div class="obs-exports">
              <button class="obs-btn ghost" onclick="obsExport('csv')" title="Download the breakdown as CSV">⬇ CSV</button>
              <button class="obs-btn ghost" onclick="obsExport('json')" title="Download the breakdown as JSON">⬇ JSON</button>
            </div>
          </div>
        </div>
        <div class="obs-strip" id="obsKpis"></div>
        <div class="obs-card"><h3>Cost projection <span class="obs-note">planning estimate from your average run</span></h3>
          <div id="obsProj"></div></div>
        <div class="obs-card"><h3 id="obsChartTitle">Cost</h3><div class="obs-chart-wrap"><canvas id="obsChart"></canvas></div></div>
        <div class="obs-card"><h3>Breakdown</h3>
          <div class="obs-tablewrap" id="obsTable"></div>
          <div class="obs-note" id="obsLegend"></div></div>
      </section>

      <section id="obsFlow" style="display:none">
        <div class="obs-card">
          <div class="obs-controls">
            <label class="grow">Session <span class="obs-note" style="text-transform:none;font-weight:500;margin:0">select or type to filter</span>
              <div class="obs-combo" id="obsCombo">
                <input id="obsSession" autocomplete="off" placeholder="Select or type a session…">
                <button type="button" class="obs-combo-caret" id="obsComboCaret" aria-label="Toggle list">▾</button>
                <div class="obs-combo-list" id="obsComboList" style="display:none"></div>
              </div>
            </label>
            <button class="obs-btn" onclick="obsLoadFlow()">Load</button>
            <div class="obs-exports">
              <button class="obs-btn ghost" onclick="obsExportFlow('csv')" title="Download this run's calls as CSV">⬇ CSV</button>
              <button class="obs-btn ghost" onclick="obsExportFlow('json')" title="Download this run's calls as JSON">⬇ JSON</button>
            </div>
          </div>
        </div>
        <div class="obs-strip" id="obsFlowKpis"></div>
        <div class="obs-card"><h3>Model rates <span class="obs-note">USD per 1M tokens, from the price book</span></h3><div id="obsRates"></div></div>
        <div class="obs-card"><h3>Cost by agent</h3><div class="obs-chart-wrap"><canvas id="obsFlowChart"></canvas></div></div>
        <div class="obs-card"><h3>Per-agent breakdown <span class="obs-note">click a row to see individual calls</span></h3>
          <div class="obs-tablewrap" id="obsFlowTable"></div>
          <div class="obs-note" id="obsFlowLegend"></div></div>
      </section>
     </div>`;
    wireCombo();
  }

  // ---- view toggle -----------------------------------------------------------
  function showObs() {
    build();
    document.querySelector("main").style.display = "none";
    $("obsView").style.display = "flex";
    $("navObs").classList.add("active"); $("navCampaigns").classList.remove("active");
    goto(tab, true);
  }
  function showCampaigns() {
    $("obsView").style.display = "none";
    document.querySelector("main").style.display = "";
    $("navCampaigns").classList.add("active"); $("navObs").classList.remove("active");
  }
  function goto(t, force) {
    tab = t;
    $("obsTabOverview").classList.toggle("active", t === "overview");
    $("obsTabFlow").classList.toggle("active", t === "flow");
    $("obsOverview").style.display = t === "overview" ? "" : "none";
    $("obsFlow").style.display = t === "flow" ? "" : "none";
    if (t === "overview") loadOverview();
    else { loadSessions(); }
  }

  // ---- overview --------------------------------------------------------------
  async function loadOverview() {
    const by = $("obsBy").value, from = $("obsFrom").value, to = $("obsTo").value;
    let data;
    try { data = await api(`/api/telemetry/aggregate?by=${by}&from=${from}&to=${to}`); }
    catch (e) { $("obsTable").innerHTML = `<div class="obs-empty">Couldn't load telemetry: ${esc(e.message)}</div>`; return; }
    const buckets = data.buckets || [];
    lastBuckets = buckets; lastBy = by;
    const tot = buckets.reduce((a, b) => ({
      cost: a.cost + Number(b.costUsd || 0), inp: a.inp + Number(b.inputTokens || 0),
      out: a.out + Number(b.outputTokens || 0), calls: a.calls + Number(b.calls || 0),
    }), { cost: 0, inp: 0, out: 0, calls: 0 });
    const runs = Number(data.totalSessions || 0);

    const bucketLabel = { date: "Active days", model: "Models used", user: "Users" }[by] || "Groups";
    $("obsKpis").innerHTML = kpi(fmtUsd(tot.cost), "Total cost", "#2563eb")
      + kpi(fmtInt(runs), "Runs", "#7c3aed")
      + kpi(fmtInt(tot.inp + tot.out), "Total tokens", "#0891b2",
            fmtInt(tot.inp) + " in · " + fmtInt(tot.out) + " out")
      + kpi(fmtInt(tot.calls), "Calls", "#059669")
      + kpi(esc(String(buckets.length)), bucketLabel, "#d97706");

    renderProjection(tot.cost, runs);

    $("obsChartTitle").textContent = "Cost by " + by;
    if (!buckets.length) { $("obsTable").innerHTML = `<div class="obs-empty">No telemetry in this range yet. Run a session, then come back.</div>`; if (charts.obsChart) charts.obsChart.destroy(); return; }

    const labels = buckets.map((b) => String(b.key));
    const costs = buckets.map((b) => Number(b.costUsd || 0));
    const asBar = by === "date";
    draw("obsChart", {
      type: asBar ? "bar" : "doughnut",
      data: {
        labels,
        datasets: [{
          label: "Cost (USD)", data: costs,
          backgroundColor: asBar ? "#2563eb" : labels.map((_, i) => PALETTE[i % PALETTE.length]),
          borderRadius: asBar ? 6 : 0,
        }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: !asBar, position: "right" },
          tooltip: { callbacks: { label: (c) => " " + fmtUsd(c.parsed.y ?? c.parsed) } } },
        scales: asBar ? { y: { ticks: { callback: (v) => "$" + v } } } : {},
      },
    });

    // In the by-model view, surface the per-1M rates that priced each model so
    // the customer can see the input/output unit price alongside the totals.
    const rateCols = by === "model";
    const rateCell = (v) => Number(v || 0) ? "$" + Number(v).toFixed(2) : "—";
    $("obsTable").innerHTML = `<table class="obs"><thead><tr>
      <th>${esc(by[0].toUpperCase() + by.slice(1))}</th><th class="num">Cost</th>
      <th class="num">Input tok</th><th class="num">Output tok</th>${
      rateCols ? '<th class="num">In $/M</th><th class="num">Out $/M</th>' : ""}
      <th class="num">Calls</th><th class="num">Latency</th></tr></thead><tbody>${
      buckets.map((b) => `<tr><td>${esc(String(b.key))}</td>
        <td class="num">${fmtUsd(b.costUsd)}</td>
        <td class="num">${fmtInt(b.inputTokens)}</td>
        <td class="num">${fmtInt(b.outputTokens)}</td>${
        rateCols ? `<td class="num">${rateCell(b.inRate)}</td><td class="num">${rateCell(b.outRate)}</td>` : ""}
        <td class="num">${fmtInt(b.calls)}</td>
        <td class="num">${fmtMs(b.latencyMs)}</td></tr>`).join("")}</tbody></table>`;
    $("obsLegend").innerHTML = LEGEND;
  }

  // ---- run detail ------------------------------------------------------------
  let sessionMap = {};        // "topic · id" -> id  (for resolveSessionId)
  let sessionsCache = [];     // [{session_id, topic, disp}]
  async function loadSessions() {
    let sessions = [];
    try { sessions = await api("/api/sessions"); } catch (e) { /* ignore */ }
    sessionMap = {};
    sessionsCache = (sessions || []).map((s) => {
      const disp = `${s.topic || s.session_id} · ${s.session_id}`;
      sessionMap[disp] = s.session_id;
      return { session_id: s.session_id, topic: s.topic || s.session_id, disp };
    });
    renderCombo("");
    const inp = $("obsSession");
    const cur = window.current || "";
    if (cur && !inp.value) {
      const match = sessionsCache.find((s) => s.session_id === cur);
      if (match) { inp.value = match.disp; loadFlow(); }
    } else if (inp.value) {
      loadFlow();
    }
  }

  // ---- combobox (dropdown + typeahead) --------------------------------------
  function renderCombo(filter) {
    const q = (filter || "").toLowerCase().trim();
    const list = q
      ? sessionsCache.filter((s) => `${s.topic} ${s.session_id}`.toLowerCase().includes(q))
      : sessionsCache;
    const box = $("obsComboList");
    if (!box) return;
    if (!sessionsCache.length) { box.innerHTML = `<div class="obs-combo-empty">No sessions yet — run one first.</div>`; return; }
    if (!list.length) { box.innerHTML = `<div class="obs-combo-empty">No match for "${esc(filter)}"</div>`; return; }
    box.innerHTML = list.map((s) =>
      `<div class="obs-combo-opt" data-id="${esc(s.session_id)}" data-disp="${esc(s.disp)}">
         <div class="ot">${esc(s.topic)}</div><div class="oid">${esc(s.session_id)}</div></div>`).join("");
    // mousedown (not click) so the pick fires before the input's blur closes the list
    box.querySelectorAll(".obs-combo-opt").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        e.preventDefault();
        pickSession(el.dataset.disp, el.dataset.id);
      });
    });
  }
  function openCombo() { renderCombo($("obsSession").value); $("obsComboList").style.display = ""; $("obsCombo").classList.add("open"); }
  function closeCombo() { const b = $("obsComboList"); if (b) b.style.display = "none"; const c = $("obsCombo"); if (c) c.classList.remove("open"); }
  function pickSession(disp, id) {
    const inp = $("obsSession");
    inp.value = disp; sessionMap[disp] = id;
    closeCombo(); loadFlow();
  }
  function wireCombo() {
    const inp = $("obsSession"), caret = $("obsComboCaret");
    if (!inp || !caret) return;
    inp.addEventListener("focus", openCombo);
    inp.addEventListener("input", () => { renderCombo(inp.value); openCombo(); });
    inp.addEventListener("blur", () => setTimeout(closeCombo, 150));
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { closeCombo(); loadFlow(); }
      else if (e.key === "Escape") { closeCombo(); }
    });
    caret.addEventListener("mousedown", (e) => {
      e.preventDefault();
      if ($("obsCombo").classList.contains("open")) closeCombo();
      else { inp.focus(); openCombo(); }
    });
  }

  function resolveSessionId() {
    const raw = ($("obsSession").value || "").trim();
    if (sessionMap[raw]) return sessionMap[raw];          // picked from list
    const m = raw.match(/([0-9a-f]{6,})\s*$/i);           // "…· id" or a raw id
    return m ? m[1] : raw;
  }

  async function loadFlow() {
    const sid = resolveSessionId();
    if (!sid) return;
    let data;
    try { data = await api(`/api/sessions/${sid}/telemetry`); }
    catch (e) { $("obsFlowTable").innerHTML = `<div class="obs-empty">Couldn't load: ${esc(e.message)}</div>`; return; }
    flowCalls = data.calls || [];
    renderRates(flowCalls);
    const t = data.totals || {};
    $("obsFlowKpis").innerHTML = kpi(fmtUsd(t.costUsd), "Flow cost", "#2563eb")
      + kpi(fmtInt((Number(t.inputTokens || 0) + Number(t.outputTokens || 0))), "Tokens", "#0891b2",
            fmtInt(t.inputTokens) + " in · " + fmtInt(t.outputTokens) + " out")
      + kpi(fmtInt(t.calls), "Calls", "#059669")
      + kpi(fmtMs(t.latencyMs), "Model+tool latency", "#d97706");

    const agents = (data.byAgent || []).slice().sort((a, b) => Number(b.costUsd || 0) - Number(a.costUsd || 0));
    if (!agents.length) { $("obsFlowTable").innerHTML = `<div class="obs-empty">No telemetry for this session yet.</div>`; if (charts.obsFlowChart) charts.obsFlowChart.destroy(); return; }

    draw("obsFlowChart", {
      type: "bar",
      data: {
        labels: agents.map((a) => a.agentId),
        datasets: [{ label: "Cost (USD)", data: agents.map((a) => Number(a.costUsd || 0)),
          backgroundColor: "#7c3aed", borderRadius: 6 }],
      },
      options: {
        indexAxis: "y", maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => " " + fmtUsd(c.parsed.x) } } },
        scales: { x: { ticks: { callback: (v) => "$" + v } } },
      },
    });

    $("obsFlowTable").innerHTML = `<table class="obs"><thead><tr>
      <th>Agent</th><th>Model(s)</th><th class="num">Input</th><th class="num">Output</th>
      <th class="num">Tools</th><th class="num">Latency</th><th class="num">Cost</th></tr></thead><tbody>${
      agents.map((a) => `
        <tr class="obs-agent" onclick="obsToggle('${esc(a.agentId)}')">
          <td><span class="caret" id="caret-${esc(a.agentId)}">▸</span> ${esc(friendlyAgent(a.agentId))}</td>
          <td>${(a.models || []).map((m) => esc(shortModel(m))).join(", ") || "—"}</td>
          <td class="num">${fmtInt(a.inputTokens)}</td>
          <td class="num">${fmtInt(a.outputTokens)}</td>
          <td class="num">${fmtInt(a.tools)}</td>
          <td class="num">${fmtMs(a.latencyMs)}</td>
          <td class="num">${fmtUsd(a.costUsd)}</td>
        </tr>
        <tr class="obs-calls" id="calls-${esc(a.agentId)}" style="display:none"><td colspan="7">
          <div class="obs-calls-inner" id="calls-inner-${esc(a.agentId)}"></div></td></tr>`).join("")}</tbody></table>`;
    $("obsFlowLegend").innerHTML = LEGEND;
  }

  function renderRates(calls) {
    const seen = {};
    (calls || []).forEach((c) => {
      if (c.kind === "llm" && c.label && !(c.label in seen)) {
        seen[c.label] = { in: Number(c.in_rate || 0), out: Number(c.out_rate || 0) };
      }
    });
    const models = Object.keys(seen);
    $("obsRates").innerHTML = models.length ? `<table class="obs"><thead><tr>
      <th>Model</th><th class="num">Input $/1M tokens</th><th class="num">Output $/1M tokens</th></tr></thead><tbody>${
      models.map((m) => `<tr><td>${esc(shortModel(m))}</td>
        <td class="num">$${seen[m].in.toFixed(2)}</td>
        <td class="num">$${seen[m].out.toFixed(2)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="obs-note">No model calls in this session.</div>`;
  }

  function toggleAgent(agentId) {
    const row = $("calls-" + agentId), caret = $("caret-" + agentId);
    if (!row) return;
    const open = row.style.display !== "none";
    row.style.display = open ? "none" : "";
    if (caret) caret.textContent = open ? "▸" : "▾";
    if (!open) {
      // Chronological within the agent. `sk` (and `ts`) are ET strings now
      // ("YYYY-MM-DD HH:MM:SS…"), which sort lexicographically = chronologically;
      // legacy rows have a numeric sk that also sorts correctly among themselves.
      const calls = flowCalls.filter((c) => c.agent_id === agentId)
        .sort((a, b) => String(a.sk || a.ts || "").localeCompare(String(b.sk || b.ts || "")));
      const rate = (c, k) => (c.kind === "llm" && Number(c[k] || 0)) ? "$" + Number(c[k]).toFixed(2) : "—";
      // System-prompt tokens: exact via Bedrock CountTokens; a "~" marks the rare
      // estimate fallback.
      const sysCell = (c) => {
        if (c.kind !== "llm") return "—";
        return (c.system_tokens_exact === false ? "~" : "") + fmtInt(c.system_tokens);
      };
      // Input column: LLM rows show exact model input tokens; tool rows have no
      // model input, but a KB tool row embeds its query (Titan) — an estimate we
      // show with a ~ marker so it is never confused with the exact model count.
      const inputCell = (c) => {
        if (c.kind === "llm") return fmtInt(c.input_tokens);
        if (Number(c.embed_tokens_est || 0)) return `<span title="estimated embedding tokens">~${fmtInt(c.embed_tokens_est)} embed</span>`;
        return "—";
      };
      $("calls-inner-" + agentId).innerHTML = calls.length ? `<table class="obs"><thead><tr>
        <th>Kind</th><th>Model / provider</th><th class="num">Input</th><th class="num">Output</th>
        <th class="num" title="The agent's system-prompt share of the input tokens — exact, via the Bedrock CountTokens API. A subset of Input. A leading ~ marks a rare estimate fallback.">Sys prompt</th>
        <th class="num">In $/M</th><th class="num">Out $/M</th>
        <th class="num">Latency</th><th class="num">Cost</th><th>Mode</th></tr></thead><tbody>${
        calls.map((c) => `<tr>
          <td><span class="obs-pill ${esc(c.kind)}">${esc(c.kind)}</span></td>
          <td>${esc(providerLabel(c))}</td>
          <td class="num">${inputCell(c)}</td>
          <td class="num">${c.kind === "llm" ? fmtInt(c.output_tokens) : "—"}</td>
          <td class="num">${sysCell(c)}</td>
          <td class="num">${rate(c, "in_rate")}</td>
          <td class="num">${rate(c, "out_rate")}</td>
          <td class="num">${fmtMs(c.latency_ms)}</td>
          <td class="num">${fmtUsd(c.cost_usd)}</td>
          <td>${c.mode === "simulated" ? '<span class="obs-pill simulated">simulated</span>' : esc(c.mode)}</td>
        </tr>`).join("")}</tbody></table>
        <div class="obs-note">${LEGEND}</div>` : `<div class="obs-note">No calls recorded.</div>`;
    }
  }

  function shortModel(m) {
    m = String(m || "");
    return m.replace(/^us\.anthropic\./, "").replace(/-\d{8}-v\d.*$/, "").replace(/:0$/, "");
  }
  // "__session__" is the synthetic bucket for AgentCore Runtime compute (not a
  // real agent, and not a model call — so it has no tokens, only a compute cost).
  function friendlyAgent(id) { return id === "__session__" ? "AgentCore Runtime · compute" : id; }
  function providerLabel(c) {
    if (c.kind === "agentcore") return "AgentCore Runtime — compute (vCPU + GB-hours)";
    return shortModel(c.label);
  }
  function kpi(v, l, accent, sub) {
    const a = accent ? ` style="--kpi-accent:${accent}"` : "";
    const s = sub ? `<div class="s">${esc(String(sub))}</div>` : "";
    return `<div class="obs-kpi"${a}><div class="v">${esc(String(v))}</div><div class="l">${esc(l)}</div>${s}</div>`;
  }

  // ---- cost projection -------------------------------------------------------
  let projAvg = 0;
  function renderProjection(totalCost, runs) {
    projAvg = runs > 0 ? totalCost / runs : 0;
    const el = $("obsProj");
    if (!el) return;
    if (!runs) { el.innerHTML = `<div class="obs-note" style="margin:0">Run a session to see per-run cost and a monthly projection.</div>`; return; }
    const perDay = Number(($("obsRunsPerDay") && $("obsRunsPerDay").value) || 10);
    el.innerHTML = `
      <div class="obs-proj">
        <div class="pcol"><span class="pv">${fmtUsd(projAvg)}</span><span class="pl">Avg cost / run</span></div>
        <div class="psep"></div>
        <div class="pcol"><span class="pv">${fmtInt(runs)}</span><span class="pl">Runs measured</span></div>
        <div class="psep"></div>
        <div class="pcol">
          <div class="prunbox"><input id="obsRunsPerDay" type="number" min="0" step="1" value="${perDay}" oninput="obsReproject()"> <span class="pl" style="text-transform:none">runs / day</span></div>
          <span class="pl">Planned volume</span>
        </div>
        <div class="psep"></div>
        <div class="pcol"><span class="pbig" id="obsProjMonthly">${fmtUsd(projAvg * perDay * 30)}</span><span class="pl">Projected / month (30 days)</span></div>
      </div>`;
  }
  function reproject() {
    const perDay = Number(($("obsRunsPerDay") && $("obsRunsPerDay").value) || 0);
    const m = $("obsProjMonthly");
    if (m) m.textContent = fmtUsd(projAvg * perDay * 30);
  }

  // ---- export (client-side download) -----------------------------------------
  function download(name, text, type) {
    const blob = new Blob([text], { type });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }
  function toCsv(rows) {
    const cell = (v) => {
      const s = String(v == null ? "" : v);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    return rows.map((r) => r.map(cell).join(",")).join("\r\n");
  }
  function stamp() { return etStamp(); }   // ET timestamp for export filenames

  function exportOverview(fmt) {
    if (!lastBuckets.length) return;
    if (fmt === "json") {
      download(`observability-${lastBy}-${stamp()}.json`,
        JSON.stringify({ groupedBy: lastBy, buckets: lastBuckets }, null, 2), "application/json");
      return;
    }
    const head = [lastBy, "costUsd", "inputTokens", "outputTokens", "calls", "runs", "latencyMs"];
    if (lastBy === "model") head.push("inRatePerM", "outRatePerM");
    const rows = [head].concat(lastBuckets.map((b) => {
      const r = [b.key, b.costUsd, b.inputTokens, b.outputTokens, b.calls, b.sessions, b.latencyMs];
      if (lastBy === "model") r.push(b.inRate, b.outRate);
      return r;
    }));
    download(`observability-${lastBy}-${stamp()}.csv`, toCsv(rows), "text/csv");
  }

  function exportFlow(fmt) {
    const sid = resolveSessionId();
    if (!flowCalls.length) return;
    if (fmt === "json") {
      download(`run-${sid}-${stamp()}.json`, JSON.stringify(flowCalls, null, 2), "application/json");
      return;
    }
    const head = ["agent_id", "kind", "label", "mode", "input_tokens", "output_tokens",
      "system_tokens", "system_tokens_exact", "embed_tokens_est", "in_rate", "out_rate", "latency_ms", "cost_usd", "ts"];
    // (embed_tokens_est is the estimated KB-query embedding token count for tool rows)
    const rows = [head].concat(flowCalls.map((c) => head.map((k) => c[k])));
    download(`run-${sid}-${stamp()}.csv`, toCsv(rows), "text/csv");
  }

  // ---- expose the handlers the inline HTML calls -----------------------------
  window.showObsView = showObs;
  window.showCampaignsView = showCampaigns;
  window.obsGoto = goto;
  window.obsLoadOverview = loadOverview;
  window.obsLoadFlow = loadFlow;
  window.obsToggle = toggleAgent;
  window.obsReproject = reproject;
  window.obsExport = exportOverview;
  window.obsExportFlow = exportFlow;
})();
