const $ = (s) => document.querySelector(s);
const state = { meta: null, day: 0, truth: false, sel: null, timer: null, cache: new Map(), dayData: null };
const REGIME = { OK: ["ok", "Stocked"], SCARCE: ["scarce", "Running short"], OUT: ["out", "Empty"] };
const WORD = { tab: "tablets", cap: "capsules", sachet: "sachets" };
const fmt = (n) => Math.round(n).toLocaleString("en-IN");
const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);

function drugName(j) {   // names come from the (Gemini-compiled) rulebook, so escape them
  const [name, strength] = state.meta.drugs[j].name.split(/_(?=[^_]+$)/);
  const base = name.split("_").map((w) => (w.length <= 3 ? w.toUpperCase() : w)).join(" ");   // ifa -> IFA, ors -> ORS
  return esc((base[0].toUpperCase() + base.slice(1)) + (strength && /^\d/.test(strength) ? ` ${strength}` : ""));
}
function place(f) {
  const [s, w, p] = state.meta.facilities[f].id.split("-");
  return { st: `State ${+s.slice(1) + 1}`, wh: `Warehouse ${String.fromCharCode(65 + +w.slice(1))}`, phc: `PHC ${+p.slice(1) + 1}` };
}
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} answered ${r.status}`);
  return r.json();
}

function buildGrid() {
  const { facilities, drugs } = state.meta;
  const head = `<thead><tr><th scope="col">PHC</th>${drugs.map((_, j) => `<th scope="col" class="drug">${drugName(j)}</th>`).join("")}</tr></thead>`;
  let body = "", lastSt = "", lastWh = "";
  facilities.forEach((fac, f) => {
    const p = place(f);
    if (p.st !== lastSt) { body += `<tr class="state"><th colspan="${drugs.length + 1}" scope="rowgroup">${p.st}</th></tr>`; lastSt = p.st; }
    if (fac.wh !== lastWh) { body += `<tr class="wh"><th colspan="${drugs.length + 1}" scope="rowgroup">${p.wh}</th></tr>`; lastWh = fac.wh; }
    body += `<tr><th scope="row" class="phc">${p.phc}</th>${drugs.map((_, j) => `<td><button type="button" class="cell" data-f="${f}" data-j="${j}"></button></td>`).join("")}</tr>`;
  });
  $("#grid").innerHTML = head + `<tbody>${body}</tbody>`;
  $("#grid").addEventListener("click", (e) => {
    const b = e.target.closest(".cell");
    if (b) select(+b.dataset.f, +b.dataset.j);
  });
}

function paintGrid(data) {
  for (const c of data.cells) {
    const b = document.querySelector(`.cell[data-f="${c.f}"][data-j="${c.j}"]`);
    const [cls, word] = REGIME[c.regime];
    b.className = `cell ${cls}${c.phantom ? " phantom" : ""}${state.sel && state.sel.f === c.f && state.sel.j === c.j ? " sel" : ""}`;
    const p = place(c.f);
    b.setAttribute("aria-label", `${p.phc}, ${p.wh}, ${drugName(c.j)}: register ${fmt(c.book)}, shelf ${word.toLowerCase()}${c.phantom ? ", register disagrees" : ""}`);
    b.innerHTML = c.true === undefined ? "" :
      `<span class="truthdot ${c.true_cover < 0.5 ? "t-out" : c.true_cover < 7 ? "t-low" : "t-ok"}" aria-hidden="true"></span>`;
  }
}

function headline(data) {
  const n = data.summary.phantom;
  let html = n
    ? `${n} ${n === 1 ? "shelf" : "shelves"} the register calls stocked ${n === 1 ? "is" : "are"} actually empty.`
    : "Every register agrees with the shelf today.";
  const c = state.sel && data.cells.find((x) => x.f === state.sel.f && x.j === state.sel.j);
  if (c && c.phantom) {
    const p = place(c.f), unit = WORD[state.meta.drugs[c.j].unit] || "units";
    html += ` At ${p.phc}, ${p.wh}, the register still reads <span class="ledger struck">${fmt(c.book)}</span> ${unit} of ${drugName(c.j).toLowerCase()}.`;
  }
  $("#phantomLine").innerHTML = html;
}

async function load(t) {
  const key = `${t}|${state.truth}`;
  if (!state.cache.has(key)) state.cache.set(key, await get(`/api/day/${t}${state.truth ? "?truth=true" : ""}`));
  return state.cache.get(key);
}

async function show(t) {
  state.day = t;
  $("#day").value = t;
  $("#dayOut").textContent = t;
  const data = await load(t);
  if (state.day !== t) return;
  state.dayData = data;
  paintGrid(data);
  headline(data);
  if (state.sel) renderDetail();
}

function select(f, j) {
  state.sel = { f, j };
  paintGrid(state.dayData);
  headline(state.dayData);
  renderDetail();
}

function chart(s) {
  const n = s.N.length, from = Math.max(0, n - 30), W = 320, H = 120, pad = 18;
  const days = s.N.slice(from).map((_, i) => from + i);
  const tot = (i) => s.full[i] + s.ration[i] + s.sub[i] + s.na[i];
  const top = Math.max(1, ...days.map((i) => Math.max(s.N[i], tot(i))));
  const bw = (W - pad) / days.length, y = (v) => H - pad - (v / top) * (H - pad * 1.5);
  const colors = [["full", "var(--ok)"], ["ration", "var(--scarce)"], ["sub", "var(--sub)"], ["na", "var(--out)"]];
  let bars = "", line = "";
  days.forEach((i, k) => {
    let base = 0;
    for (const [key, col] of colors) {
      const v = s[key][i];
      if (v > 0) bars += `<rect x="${pad + k * bw + 1}" y="${y(base + v)}" width="${bw - 2}" height="${y(base) - y(base + v)}" fill="${col}"/>`;
      base += v;
    }
    line += `${k ? "L" : "M"}${pad + k * bw + bw / 2},${y(s.N[i])}`;
  });
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Last ${days.length} days: courses the diagnoses called for, against what was dispensed">
    <line x1="${pad}" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="var(--rule)"/>
    ${bars}<path d="${line}" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="4 3"/>
    <text x="${pad}" y="${H - 4}" font-size="10" fill="var(--muted)">day ${days[0]}</text>
    <text x="${W}" y="${H - 4}" font-size="10" fill="var(--muted)" text-anchor="end">day ${days[days.length - 1]}</text>
  </svg>`;
}

function tree(c, p) {
  if (c.level === "DEMAND-SURGE")
    return `<p class="why">Diagnoses for the conditions this medicine treats jumped while deliveries kept arriving. This looks like a demand surge, not a supply failure.</p>`;
  const levels = ["LOCAL", "WAREHOUSE", "STATE-PROCUREMENT", "NATIONAL"];
  const names = [`This PHC (${p.phc})`, p.wh, `${p.st} medical services corporation`, "National supply"];
  const at = levels.indexOf(c.level);
  return `<ol class="tree">${names.map((nm, i) => `<li class="${i < at ? "hit" : i === at ? "broke" : ""}">${nm}${i === at ? " is where it broke" : ""}</li>`).join("")}</ol>`;
}

async function renderDetail() {
  const { f, j } = state.sel, t = state.day;
  const c = state.dayData.cells.find((x) => x.f === f && x.j === j);
  const s = await get(`/api/series/${f}/${j}?t=${t}${state.truth ? "&truth=true" : ""}`);
  if (state.day !== t || state.sel.f !== f || state.sel.j !== j) return;
  const p = place(f), [cls, word] = REGIME[c.regime], unit = WORD[state.meta.drugs[j].unit] || "units";
  const sum = (k) => s[k].slice(-14).reduce((a, b) => a + b, 0);
  const sure = Math.min(99, Math.round(100 * Math.max(...c.p)));   // never claim certainty
  const truth = c.true === undefined ? "" :
    `<p class="why"><strong>Ground truth:</strong> ${fmt(c.true)} ${unit} on the shelf (${c.true_cover} days of use).</p>`;
  const alarm = !c.alarm ? `<p class="why">No alarm. Care at this PHC matches what the diagnoses call for.</p>` : `
    <p class="why">Alarm since day ${c.onset}. ${c.level === "LOCAL" ? "Only this PHC is affected so far." : c.level === "DEMAND-SURGE" ? "" : "Other PHCs went short on the same medicine in the same weeks."}</p>
    ${tree(c, p)}
    <p class="action ${["LOCAL", "WAREHOUSE", "DEMAND-SURGE"].includes(c.level) ? "local" : ""}"><strong>Suggested next step:</strong> ${esc(state.meta.actions[c.level])}. Treat where it broke as a first guess to check, not a verdict.</p>`;
  const asked = c.confirmed ? `<p class="done">The pharmacist said: ${c.confirmed === "empty" ? "it's finished" : "we have it"}.</p>` : "";
  $("#detail").innerHTML = `
    <h2>${drugName(j)}</h2>
    <p class="where">${p.phc}, ${p.wh}, ${p.st}, day ${t}</p>
    <div class="versus">
      <div class="reg"><small>Register says</small><span class="ledger big${c.phantom ? " struck" : ""}">${fmt(c.book)}</span><small>${unit}, about ${c.cover} days of use</small></div>
      <div class="shelf ${cls}"><small>Shelf, inferred from care</small><span class="big state-${cls}">${word}</span><small>${sure}% sure</small></div>
    </div>
    ${truth}
    <p class="why">In the last 14 days the diagnoses here called for about ${fmt(sum("N"))} courses.
      ${fmt(sum("full"))} were given in full, ${fmt(sum("ration"))} were cut short, ${fmt(sum("sub"))} were switched to a guideline substitute and ${fmt(sum("na"))} were marked not available.</p>
    ${chart(s)}
    <div class="chart-key" aria-hidden="true">
      <span><i class="sw ok"></i>Full course</span><span><i class="sw scarce"></i>Cut short</span>
      <span><i class="sw" style="background:var(--sub)"></i>Substitute</span><span><i class="sw out"></i>Not available</span>
      <span>- - - courses the diagnoses called for</span>
    </div>
    ${alarm}
    <div class="confirm">
      <p><strong>Ask the pharmacist</strong> to check the shelf. Their answer updates the estimate.</p>
      ${asked}
      <div class="buttons">
        <button type="button" class="yes" data-answer="empty">Yes, it's finished<span class="or" lang="or">ହଁ, ସରିଯାଇଛି</span></button>
        <button type="button" data-answer="available">No, we have it<span class="or" lang="or">ନା, ଅଛି</span></button>
      </div>
      <p id="confirmMsg" role="status"></p>
    </div>`;
  $("#detail").querySelectorAll(".confirm button").forEach((b) => b.addEventListener("click", () => confirmShelf(b.dataset.answer)));
}

async function confirmShelf(answer) {
  const { f, j } = state.sel;
  try {
    const r = await fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ f, j, t: state.day, answer }) });
    if (!r.ok) throw new Error(`the server answered ${r.status}`);
    state.cache.clear();
    await show(state.day);
    $("#confirmMsg").innerHTML = `<span class="done">Answer saved.</span> The estimate now includes it.`;
  } catch (e) {
    $("#confirmMsg").textContent = `Could not save the answer: ${e.message}.`;
  }
}

function proof(m) {
  const days = (n) => `${n} ${n === 1 ? "day" : "days"}`;
  const lead = (d) => d === null || Number.isNaN(d) ? "n/a" : d > 0 ? `${days(d)} before` : d < 0 ? `${days(-d)} after` : "same day";
  const pct = (x) => (x === null || Number.isNaN(x) ? "n/a" : `${Math.round(100 * x)}%`);
  const rows = [["Anumaan", m.model],
    [`The register, tuned to the same false-alarm budget (${m.register_best ? m.register_best.threshold : "?"} days)`, m.register_best],
    ["The register at 14 days, a common reorder level", m.register["14"]],
    ["Dispensing history only, no diagnoses", m.drug_only],
    ["Simple threshold on the same rulebook", m.crg_rule]].filter(([, r]) => r);
  $("#proof").innerHTML = `<thead><tr><th scope="col">Method</th><th scope="col">Stock-outs of 4+ days caught</th><th scope="col">Warned before the shelf emptied</th><th scope="col">Typical warning</th><th scope="col">False alarms per medicine per year</th></tr></thead>
    <tbody>${rows.map(([n, r]) => `<tr><th scope="row">${n}</th><td>${pct(r.recall_4d)}</td><td>${pct(r.early)}</td><td>${lead(r.median_lead)}</td><td>${r.false_per_series_year.toFixed(2)}</td></tr>`).join("")}</tbody>`;
  $("#triageNote").textContent = `Where it broke is still a first guess: right for ${pct(m.triage_7d.episodes)} of supply failures a week after the alarm, against ${pct(m.triage_majority)} for always guessing the commonest cause. Early warning depends on staff rationing before the shelf empties; without it, Anumaan still catches most outages, about two days after they start.`;
}

async function init() {
  state.meta = await get("/api/meta");
  $("#day").max = state.meta.days - 1;
  $("#grammarNote").textContent = `Rulebook ${state.meta.grammar}: ${state.meta.grammar_note}`;
  buildGrid();
  proof(await get("/api/eval"));
  const s = state.meta.start;
  state.sel = { f: s.f, j: s.j };
  await show(s.day);
}

let pending;
$("#day").addEventListener("input", (e) => { clearTimeout(pending); pending = setTimeout(() => show(+e.target.value), 60); });
$("#truth").addEventListener("change", (e) => { state.truth = e.target.checked; show(state.day); });
$("#play").addEventListener("click", () => {
  const b = $("#play");
  if (state.timer) { clearInterval(state.timer); state.timer = null; b.textContent = "Play"; b.setAttribute("aria-pressed", "false"); return; }
  b.textContent = "Pause"; b.setAttribute("aria-pressed", "true");
  state.timer = setInterval(() => {
    if (state.day >= state.meta.days - 1) return $("#play").click();
    show(state.day + 1);
  }, 700);
});
init().catch((e) => { $("#detail").innerHTML = `<p class="hint">The demo could not start: ${e.message}. Check that the server is running.</p>`; });
