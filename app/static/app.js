const $ = (s) => document.querySelector(s);
const state = { meta: null, day: 0, truth: false, sel: null, timer: null, rec: null,
  cache: new Map(), side: new Map(), dayData: null };
const REGIME = { OK: ["ok", "Stocked"], SCARCE: ["scarce", "Running short"], OUT: ["out", "Empty"] };
const WORD = { tab: "tablets", cap: "capsules", sachet: "sachets" };
const CADRE = { MO: "Medical officer", SN: "Staff nurse", PH: "Pharmacist", LT: "Lab technician" };
const fmt = (n) => Math.round(n).toLocaleString("en-IN");
const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
const pct = (x) => (x === null || x === undefined || Number.isNaN(x) ? "n/a" : `${Math.round(100 * x)}%`);

function drugName(j) {   // names come from the (Gemini-compiled) rulebook, so escape them
  const [name, strength] = state.meta.drugs[j].name.split(/_(?=[^_]+$)/);
  const base = name.split("_").map((w) => (w.length <= 3 ? w.toUpperCase() : w)).join(" ");   // ifa -> IFA, ors -> ORS
  return esc((base[0].toUpperCase() + base.slice(1)) + (strength && /^\d/.test(strength) ? ` ${strength}` : ""));
}
const drugOf = (name) => state.meta.drugs.findIndex((d) => d.name === name);
const unitOf = (j) => WORD[state.meta.drugs[j].unit] || "units";
function place(f) {
  const [s, w, p] = state.meta.facilities[f].id.split("-");
  return { st: `State ${+s.slice(1) + 1}`, wh: `Warehouse ${String.fromCharCode(65 + +w.slice(1))}`, phc: `PHC ${+p.slice(1) + 1}` };
}
const facOf = (id) => state.meta.facilities.findIndex((x) => x.id === id);
const stateName = (id) => `State ${+id.slice(1) + 1}`;
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} answered ${r.status}`);
  return r.json();
}
async function cached(map, key, url) {
  if (!map.has(key)) map.set(key, await get(url));
  return map.get(key);
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
    const p = place(c.f);
    html += ` At ${p.phc}, ${p.wh}, the register still reads <span class="ledger struck">${fmt(c.book)}</span> ${unitOf(c.j)} of ${drugName(c.j).toLowerCase()}.`;
  }
  $("#phantomLine").innerHTML = html;
}

async function show(t) {
  state.day = t;
  $("#day").value = t;
  $("#dayOut").textContent = t;
  const data = await cached(state.cache, `${t}|${state.truth}`, `/api/day/${t}${state.truth ? "?truth=true" : ""}`);
  if (state.day !== t) return;
  state.dayData = data;
  paintGrid(data);
  headline(data);
  if (state.sel) renderDetail();
  const [plan, nat] = await Promise.all([cached(state.side, `plan|${t}`, `/api/plan?t=${t}`),
    cached(state.side, `nat|${t}`, `/api/national?t=${t}`)]);
  if (state.day !== t) return;
  renderMoves(plan);
  renderNational(nat);
}

function select(f, j) {
  state.sel = { f, j };
  paintGrid(state.dayData);
  headline(state.dayData);
  renderDetail();
  const plan = state.side.get(`plan|${state.day}`);
  if (plan) renderMoves(plan);
}

function svg(W, H, label, body) {
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">${body}</svg>`;
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
  return svg(W, H, `Last ${days.length} days: courses the diagnoses called for, against what was dispensed`, `
    <line x1="${pad}" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="var(--rule)"/>
    ${bars}<path d="${line}" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="4 3"/>
    <text x="${pad}" y="${H - 4}" font-size="10" fill="var(--muted)">day ${days[0]}</text>
    <text x="${W}" y="${H - 4}" font-size="10" fill="var(--muted)" text-anchor="end">day ${days[days.length - 1]}</text>`);
}

function forecastChart(s, fc) {
  const hist = s.use.slice(-30), W = 320, H = 100, pad = 18, n = hist.length + fc.mean.length;
  const top = Math.max(1, ...hist, ...fc.mean, ...fc.hi.filter((v) => v !== null));
  const x = (i) => pad + (i / (n - 1)) * (W - pad), y = (v) => H - pad - (v / top) * (H - pad * 1.5);
  const path = (vals, off) => vals.map((v, i) => `${i ? "L" : "M"}${x(i + off)},${y(v)}`).join("");
  const band = fc.lo[0] === null ? "" :
    `<path d="${path(fc.hi, hist.length)}L${fc.lo.map((v, i) => `${x(hist.length + fc.lo.length - 1 - i)},${y(fc.lo[fc.lo.length - 1 - i])}`).join("L")}Z" fill="var(--carbon)" opacity=".12"/>`;
  return svg(W, H, `Expected use over the last ${hist.length} days and the next ${fc.mean.length}`, `
    <line x1="${pad}" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="var(--rule)"/>
    <line x1="${x(hist.length - 0.5)}" x2="${x(hist.length - 0.5)}" y1="4" y2="${H - pad}" stroke="var(--rule)"/>
    ${band}<path d="${path(hist, 0)}" fill="none" stroke="var(--ink)" stroke-width="1.5"/>
    <path d="${path(fc.mean, hist.length)}" fill="none" stroke="var(--carbon)" stroke-width="2" stroke-dasharray="5 3"/>
    <text x="${pad}" y="${H - 4}" font-size="10" fill="var(--muted)">last 30 days</text>
    <text x="${W}" y="${H - 4}" font-size="10" fill="var(--muted)" text-anchor="end">next ${fc.mean.length} days</text>`);
}

function tree(c, p) {
  if (c.level === "DEMAND-SURGE")
    return `<p class="why">Diagnoses for the conditions this medicine treats jumped while deliveries kept arriving. This looks like a demand surge, not a supply failure.</p>`;
  const levels = ["LOCAL", "WAREHOUSE", "STATE-PROCUREMENT", "NATIONAL"];
  const names = [`This PHC (${p.phc})`, p.wh, `${p.st} medical services corporation`, "National supply"];
  const at = levels.indexOf(c.level);
  return `<ol class="tree">${names.map((nm, i) => `<li class="${i < at ? "hit" : i === at ? "broke" : ""}">${nm}${i === at ? " is where it broke" : ""}</li>`).join("")}</ol>`;
}

function atWork(r) {
  if (!r.in_position) return "Nobody in post";
  if (r.verify) return `<span class="flag">Marked present, but no ${esc(CADRE[r.cadre].toLowerCase())} work on a busy day. Check.</span>`;
  if (r.expected < 3) return "Too few patients today to tell";
  return r.p_present >= 0.5 ? `Yes, ${r.acts} tasks recorded` : `Probably not: ${r.acts} tasks where about ${Math.round(r.expected)} were expected`;
}

function facilityBlock(fa, p) {
  const b = fa.beds;
  const early = b.early_share_7d === null ? "" : ` ${pct(b.early_share_7d)} of last week's discharges were early.`;
  const truth = b.true_occupied === undefined ? "" : ` <strong>Ground truth:</strong> ${b.true_occupied} occupied.`;
  const rows = fa.staff.map((r) => `<tr><th scope="row">${CADRE[r.cadre]}</th><td>${r.in_position} of ${r.sanctioned}</td>
    <td>${r.in_position ? (r.marked_present ? "Present" : "Absent") : "n/a"}</td><td>${atWork(r)}</td>
    ${r.true_present === undefined ? "" : `<td>${r.true_present ? "At work" : "Away"}</td>`}</tr>`).join("");
  return `<h3>${p.phc} today</h3>
    <p class="why">Beds: <strong>${b.occupied} of ${b.capacity}</strong> occupied, counted from admissions and recorded discharges${b.pressure ? ". Every bed is taken" : ""}.${early}${truth}</p>
    <table class="data staff"><thead><tr><th scope="col">Staff</th><th scope="col">In post</th><th scope="col">Attendance</th><th scope="col">At work, from care records</th>${fa.staff[0].true_present === undefined ? "" : `<th scope="col">Ground truth</th>`}</tr></thead><tbody>${rows}</tbody></table>
    <p class="note">Staff are shown by role only. No individuals and no location tracking.</p>`;
}

async function renderDetail() {
  const { f, j } = state.sel, t = state.day;
  const c = state.dayData.cells.find((x) => x.f === f && x.j === j);
  const tq = state.truth ? "&truth=true" : "";
  const [s, fc, fa] = await Promise.all([get(`/api/series/${f}/${j}?t=${t}${tq}`),
    get(`/api/forecast/${f}/${j}?t=${t}`), get(`/api/facility/${f}?t=${t}${tq}`)]);
  if (state.day !== t || state.sel.f !== f || state.sel.j !== j) return;
  const p = place(f), [cls, word] = REGIME[c.regime], unit = unitOf(j);
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
        <button type="button" class="rec" aria-pressed="false">Record the answer<span class="or">Any language</span></button>
      </div>
      <p id="confirmMsg" role="status"></p>
    </div>
    <h3>Next ${fc.days.length} days</h3>
    <p class="why">The diagnoses point to about <strong>${fmt(fc.total)}</strong> ${unit} of use. Dispensing history, with the stock-out days filled in, suggests about ${fmt(fc.consumption_total)}.</p>
    ${forecastChart(s, fc)}
    ${facilityBlock(fa, p)}`;
  $("#detail").querySelectorAll(".confirm button[data-answer]").forEach((b) => b.addEventListener("click", () => confirmShelf(b.dataset.answer)));
  $("#detail .rec").addEventListener("click", (e) => recordAnswer(e.currentTarget));
}

function refreshAfterAnswer() {
  state.cache.clear();
  state.side.clear();
  return show(state.day);
}

async function confirmShelf(answer) {
  const { f, j } = state.sel;
  try {
    const r = await fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ f, j, t: state.day, answer }) });
    if (!r.ok) throw new Error(`the server answered ${r.status}`);
    await refreshAfterAnswer();
    $("#confirmMsg").innerHTML = `<span class="done">Answer saved.</span> The estimate now includes it.`;
  } catch (e) {
    $("#confirmMsg").textContent = `Could not save the answer: ${e.message}.`;
  }
}

async function recordAnswer(btn) {
  const say = (text) => { $("#confirmMsg").textContent = text; };   // transcripts are untrusted: text only
  if (state.rec) return state.rec.stop();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    return say("The browser did not allow the microphone. Use the buttons instead.");
  }
  const rec = new MediaRecorder(stream), chunks = [], { f, j } = state.sel, t = state.day;
  rec.ondataavailable = (e) => chunks.push(e.data);
  rec.onstop = async () => {
    stream.getTracks().forEach((tr) => tr.stop());
    state.rec = null;
    btn.setAttribute("aria-pressed", "false");
    btn.firstChild.textContent = "Record the answer";
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    say("Listening to the answer...");
    const r = await fetch(`/api/voice?f=${f}&j=${j}&t=${t}`, { method: "POST", headers: { "Content-Type": blob.type }, body: blob });
    if (r.status === 503) return say("Voice answers need Gemini on Vertex AI, which this copy of the demo is not connected to. Use the buttons.");
    if (r.status === 429) return say("Voice answers are used up for today on this demo. Use the buttons.");
    if (!r.ok) return say(`The recording could not be used (error ${r.status}). Record again, or use the buttons.`);
    const h = await r.json();
    if (h.answer === "unclear") return say(`Heard: "${h.transcript_en}". The answer was not clear. Ask again, or use the buttons.`);
    await refreshAfterAnswer();
    say(`Heard: "${h.transcript_en}". Saved as ${h.answer === "empty" ? "finished" : "in stock"}. Likely reason: ${h.cause.replace(/_/g, " ")}.`);
  };
  rec.start();
  state.rec = rec;
  btn.setAttribute("aria-pressed", "true");
  btn.firstChild.textContent = "Stop and send";
  say("Recording. Ask whether the medicine is finished, then press Stop and send.");
}

function renderMoves(plan) {
  const sel = state.sel && state.meta.facilities[state.sel.f].id;
  const moves = [...plan.transfers].sort((a, b) => (b.to_fac === sel || b.from_fac === sel) - (a.to_fac === sel || a.from_fac === sel) || b.courses - a.courses);
  const where = (id) => { const p = place(facOf(id)); return `${p.phc}, ${p.wh}`; };
  const shown = moves.slice(0, 8).map((m) => {
    const j = drugOf(m.drug), mine = m.to_fac === sel || m.from_fac === sel;
    return `<li class="${mine ? "mine" : ""}">Send <strong>${m.courses} courses</strong> (${fmt(m.units)} ${unitOf(j)}) of ${drugName(j)} from ${where(m.from_fac)} to ${where(m.to_fac)} in ${place(facOf(m.to_fac)).st}, ${m.minutes} minutes by road.</li>`;
  }).join("");
  const more = moves.length > 8 ? `<p class="note">And ${moves.length - 8} smaller transfers. Each goes out as a DVDMS-style issue order.</p>` : "";
  const groups = {};
  for (const e of plan.escalations) {
    const k = `${e.drug}|${e.level}|${e.fac.split("-")[0]}`;
    groups[k] = (groups[k] || 0) + 1;
  }
  const escRows = Object.entries(groups).map(([k, n]) => {
    const [drug, level, st] = k.split("|");
    return `<li>${drugName(drugOf(drug))} in ${stateName(st)}: ${n} ${n === 1 ? "PHC" : "PHCs"} short, looks like a ${level === "NATIONAL" ? "national shortage" : "state procurement gap"}. ${esc(state.meta.actions[level])}.</li>`;
  }).join("");
  $("#moves").innerHTML = (shown ? `<ul class="plain">${shown}</ul>${more}` : `<p class="note">Nothing to move today. No PHC in a local shortage has a calm neighbour with stock to spare.</p>`)
    + (escRows ? `<h3>Escalate instead of moving stock</h3><ul class="plain">${escRows}</ul>` : "");
}

function renderNational(n) {
  const rows = n.exports.map((ex) => {
    const tot = (k) => ex.rows.reduce((a, r) => a + r[k], 0);
    return `<tr><th scope="row">${stateName(ex.state)}</th><td>${fmt(tot("OK"))}</td><td>${fmt(tot("SCARCE"))}</td><td>${fmt(tot("OUT"))}</td>
      <td>${fmt(n.raw_rows[ex.state])} records</td><td>${(n.export_bytes[ex.state] / 1024).toFixed(1)} KB of counts</td></tr>`;
  }).join("");
  const flags = Object.entries(n.view).flatMap(([drug, v]) => {
    const j = drugOf(drug), out = [];
    if (v.national) out.push(`<li>${drugName(j)}: short in several districts across states. Possible national supply failure. This flag is experimental and missed every national failure in testing.</li>`);
    if (v.surge) out.push(`<li>${drugName(j)}: demand is surging across states. Raise indents.</li>`);
    return out;
  }).join("");
  const priors = Object.entries(n.priors).map(([drug, p]) =>
    `<tr><th scope="row">${drugName(drugOf(drug))}</th><td>${pct(p.rho)}</td><td>${pct(p.tau)}</td></tr>`).join("");
  $("#national").innerHTML = `<div class="proof-wrap"><table class="data"><thead><tr><th scope="col">State</th><th scope="col">PHC medicines stocked</th><th scope="col">Running short</th><th scope="col">Empty</th><th scope="col">Kept inside the state</th><th scope="col">Sent to the national view</th></tr></thead><tbody>${rows}</tbody></table></div>
    <h3>Patterns across states</h3>${flags ? `<ul class="plain">${flags}</ul>` : `<p class="note">No medicine shows a cross-state pattern today.</p>`}
    <details><summary>What states get back</summary>
      <p class="note">National medians a new state can start from instead of waiting a month: how closely prescribing follows the rulebook, and how far registers can be trusted. In testing this made little difference to detection.</p>
      <div class="proof-wrap"><table class="data"><thead><tr><th scope="col">Medicine</th><th scope="col">Prescribing follows the rulebook</th><th scope="col">Register trust</th></tr></thead><tbody>${priors}</tbody></table></div>
    </details>`;
}

function proof(m) {
  const days = (n) => `${n} ${n === 1 ? "day" : "days"}`;
  const lead = (d) => d === null || Number.isNaN(d) ? "n/a" : d > 0 ? `${days(d)} before` : d < 0 ? `${days(-d)} after` : "same day";
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
init().catch((e) => { $("#detail").innerHTML = `<p class="hint">The demo could not start: ${esc(e.message)}. Check that the server is running.</p>`; });
