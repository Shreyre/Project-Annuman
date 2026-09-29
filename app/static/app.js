const $ = (s) => document.querySelector(s);
const state = { meta: null, day: 0, truth: false, sel: null, timer: null, rec: null,
  cache: new Map(), side: new Map(), dayData: null, cells: [], byKey: new Map(), prevPhantom: null, prevDay: null, fresh: false };
const REGIME = { OK: ["ok", "Stocked"], SCARCE: ["scarce", "Running short"], OUT: ["out", "Empty"] };
const WORD = { tab: "tablets", cap: "capsules", sachet: "sachets" };
const CADRE = { MO: "Medical officer", SN: "Staff nurse", PH: "Pharmacist", LT: "Lab technician" };
const fmt = (n) => Math.round(n).toLocaleString("en-IN");
const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
const pct = (x) => (x === null || x === undefined || Number.isNaN(x) ? "n/a" : `${Math.round(100 * x)}%`);
const cap = (s) => s[0].toUpperCase() + s.slice(1);
const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function drugName(j) {   // names come from the (Gemini-compiled) rulebook, so escape them
  const [name, strength] = state.meta.drugs[j].name.split(/_(?=[^_]+$)/);
  const base = name.split("_").map((w) => (w.length <= 3 ? w.toUpperCase() : w)).join(" ");   // ifa -> IFA, ors -> ORS
  return esc((base[0].toUpperCase() + base.slice(1)) + (strength && /^\d/.test(strength) ? ` ${strength}` : ""));
}
const drugOf = (name) => state.meta.drugs.findIndex((d) => d.name === name);
const unitOf = (j) => WORD[state.meta.drugs[j].unit] || "units";
function place(f) {
  const [s, w, p] = state.meta.facilities[f].id.split("-");
  return { st: `State\u00a0${+s.slice(1) + 1}`, wh: `Warehouse\u00a0${String.fromCharCode(65 + +w.slice(1))}`, phc: `PHC\u00a0${+p.slice(1) + 1}` };   // never split "PHC 6" across lines
}
const facOf = (id) => state.meta.facilities.findIndex((x) => x.id === id);
const stateName = (id) => `State\u00a0${+id.slice(1) + 1}`;
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} answered ${r.status}`);
  return r.json();
}
async function cached(map, key, url) {
  if (!map.has(key)) map.set(key, await get(url));
  return map.get(key);
}

/* ---------- the network grid: one table per state, warehouses as row groups ---------- */
function buildGrid() {
  const { facilities, drugs } = state.meta;
  const head = `<thead><tr><td colspan="2"></td>${drugs.map((_, j) => `<th scope="col" class="drug" data-j="${j}"><span>${drugName(j)}</span></th>`).join("")}</tr></thead>`;
  const byState = new Map();
  facilities.forEach((fac, f) => {
    if (!byState.has(fac.st)) byState.set(fac.st, new Map());
    const whs = byState.get(fac.st);
    if (!whs.has(fac.wh)) whs.set(fac.wh, []);
    whs.get(fac.wh).push(f);
  });
  let html = "";
  for (const [st, whs] of byState) {
    let body = "", r = 0;
    for (const fs of whs.values()) {
      body += "<tbody>" + fs.map((f, i) => {
        const p = place(f);
        const wh = i ? "" : `<th scope="rowgroup" rowspan="${fs.length}" class="wh"><span>${p.wh}</span></th>`;
        const row = `<tr>${wh}<th scope="row" class="phc">${p.phc}</th>${drugs.map((_, j) =>
          `<td><button type="button" class="cell" data-f="${f}" data-j="${j}" style="--r:${r};--c:${j}"></button></td>`).join("")}</tr>`;
        r++;
        return row;
      }).join("") + "</tbody>";
    }
    html += `<div class="state-block"><h3>${stateName(st)} <span class="st-count" data-st="${st}"></span></h3>
      <table class="grid">${head}${body}</table></div>`;
  }
  const grid = $("#grid");
  grid.innerHTML = html;
  grid.querySelectorAll(".cell").forEach((b) => { state.cells[+b.dataset.f * drugs.length + +b.dataset.j] = b; });
  grid.addEventListener("click", (e) => {
    const b = e.target.closest(".cell");
    if (b) select(+b.dataset.f, +b.dataset.j, true);
  });
  grid.addEventListener("pointerover", (e) => { const b = e.target.closest(".cell"); if (b && e.pointerType === "mouse") showTip(b); });
  grid.addEventListener("pointerout", (e) => { if (e.target.closest(".cell")) hideTip(); });
  grid.addEventListener("focusin", (e) => { const b = e.target.closest(".cell"); if (b) showTip(b); });
  grid.addEventListener("focusout", hideTip);
  addEventListener("scroll", hideTip, { passive: true });
}

function paintGrid(data) {
  const J = state.meta.drugs.length, count = { ok: 0, scarce: 0, out: 0, phantom: 0 }, perState = {};
  const phantoms = new Set(), stepped = state.prevPhantom && Math.abs(data.day - state.prevDay) === 1;
  state.byKey.clear();
  for (const c of data.cells) {
    const b = state.cells[c.f * J + c.j], key = c.f * J + c.j;
    const [cls, word] = REGIME[c.regime];
    state.byKey.set(key, c);
    if (c.phantom) {
      phantoms.add(key);
      count.phantom++;
      const st = state.meta.facilities[c.f].st;
      perState[st] = (perState[st] || 0) + 1;
    } else count[cls]++;
    const isNew = c.phantom && stepped && !state.prevPhantom.has(key);
    const sel = state.sel && state.sel.f === c.f && state.sel.j === c.j;
    b.className = `cell ${cls}${c.phantom ? " phantom" : ""}${sel ? " sel" : ""}${isNew && !calm() ? " flash" : ""}`;
    const p = place(c.f);
    b.setAttribute("aria-label", `${p.phc}, ${p.wh}, ${drugName(c.j)}: register ${fmt(c.book)}, shelf ${word.toLowerCase()}${c.phantom ? ", register disagrees" : ""}`);
    b.innerHTML = c.true === undefined ? "" :
      `<span class="truthdot ${c.true_cover < 0.5 ? "t-out" : c.true_cover < 7 ? "t-low" : "t-ok"}" aria-hidden="true"></span>`;
  }
  state.prevPhantom = phantoms;
  state.prevDay = data.day;
  for (const [k, n] of Object.entries(count)) $(`#legend b[data-k="${k}"]`).textContent = n;
  document.querySelectorAll(".st-count").forEach((el) => {
    const n = perState[el.dataset.st] || 0;
    el.textContent = n ? `${n} hidden ${n === 1 ? "stock-out" : "stock-outs"}` : "Registers agree";
    el.classList.toggle("has", n > 0);
  });
}

const tip = $("#tip");
function mark(b, on) {
  b.closest("table").querySelector(`th.drug[data-j="${b.dataset.j}"]`).classList.toggle("hl", on);
  b.closest("tr").querySelector("th.phc").classList.toggle("hl", on);
}
function showTip(b) {
  const c = state.byKey.get(+b.dataset.f * state.meta.drugs.length + +b.dataset.j);
  if (!c) return;
  const p = place(c.f), [, word] = REGIME[c.regime], unit = unitOf(c.j);
  tip.innerHTML = `<strong>${drugName(c.j)}</strong><span>${p.phc}, ${p.wh}</span>
    <span>Register: ${fmt(c.book)} ${unit}</span><span${c.phantom ? ' class="alert"' : ""}>Shelf: ${word.toLowerCase()}${c.phantom ? ", register disagrees" : ""}</span>
    ${c.true === undefined ? "" : `<span>Ground truth: ${fmt(c.true)} ${unit}</span>`}`;
  tip.hidden = false;
  const r = b.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  const below = r.top < h + 16 + 64;
  tip.classList.toggle("below", below);
  tip.style.left = `${Math.min(Math.max(r.left + r.width / 2, w / 2 + 8), innerWidth - w / 2 - 8)}px`;
  tip.style.top = `${below ? r.bottom : r.top}px`;
  document.querySelectorAll("th.hl").forEach((th) => th.classList.remove("hl"));
  mark(b, true);
}
function hideTip() {
  tip.hidden = true;
  document.querySelectorAll("th.hl").forEach((th) => th.classList.remove("hl"));
}

function headline(data, draw = false) {
  const n = data.summary.phantom;
  let html = n
    ? `${n} ${n === 1 ? "shelf" : "shelves"} the register calls stocked ${n === 1 ? "is" : "are"} actually empty.`
    : "Every register agrees with the shelf today.";
  const c = state.sel && data.cells.find((x) => x.f === state.sel.f && x.j === state.sel.j);
  if (c && c.phantom) {
    const p = place(c.f);
    html += ` At ${p.phc}, ${p.wh}, the register still reads <span class="ledger struck">${fmt(c.book)}</span> ${unitOf(c.j)} of ${drugName(c.j).toLowerCase()}.`;
  }
  const line = $("#phantomLine");
  line.classList.toggle("draw", draw);
  line.innerHTML = html;
}

function setDay(t) {
  $("#day").value = t;
  $("#dayOut").textContent = t;
  $("#day").style.setProperty("--p", `${(100 * t) / (state.meta.days - 1)}%`);
}

async function show(t) {
  state.day = t;
  setDay(t);
  const data = await cached(state.cache, `${t}|${state.truth}`, `/api/day/${t}${state.truth ? "?truth=true" : ""}`);
  if (state.day !== t) return;
  state.dayData = data;
  paintGrid(data);
  headline(data, state.fresh);
  if (state.sel) renderDetail();
  const [plan, nat] = await Promise.all([cached(state.side, `plan|${t}`, `/api/plan?t=${t}`),
    cached(state.side, `nat|${t}`, `/api/national?t=${t}`)]);
  if (state.day !== t) return;
  renderMoves(plan);
  renderNational(nat);
}

function select(f, j, byUser = false) {
  state.sel = { f, j };
  state.fresh = true;
  paintGrid(state.dayData);
  headline(state.dayData, true);
  renderDetail();
  const plan = state.side.get(`plan|${state.day}`);
  if (plan) renderMoves(plan);
  if (byUser && matchMedia("(max-width: 960px)").matches)
    $("#detail").scrollIntoView({ behavior: calm() ? "auto" : "smooth", block: "start" });
}

/* ---------- charts ---------- */
function svg(W, H, label, body) {
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">${body}</svg>`;
}

function chart(s) {
  const n = s.N.length, from = Math.max(0, n - 30), W = 320, H = 124, pad = 18;
  const days = s.N.slice(from).map((_, i) => from + i);
  const tot = (i) => s.full[i] + s.ration[i] + s.sub[i] + s.na[i];
  const top = Math.max(1, ...days.map((i) => Math.max(s.N[i], tot(i))));
  const bw = (W - pad) / days.length, y = (v) => H - pad - (v / top) * (H - pad * 1.5);
  const colors = [["full", "var(--ok)"], ["ration", "var(--scarce)"], ["sub", "var(--sub)"], ["na", "var(--out)"]];
  let bars = "", line = "";
  days.forEach((i, k) => {
    let base = 0, rects = "";
    for (const [key, col] of colors) {
      const v = s[key][i];
      if (v > 0) rects += `<rect x="${pad + k * bw + 1}" y="${y(base + v)}" width="${bw - 2}" height="${y(base) - y(base + v)}" fill="${col}" stroke="var(--surface)" stroke-width="1"/>`;
      base += v;
    }
    bars += `<g class="bar" style="--k:${k}"><title>Day ${i}: ${fmt(s.N[i])} courses called for. ${fmt(s.full[i])} full, ${fmt(s.ration[i])} cut short, ${fmt(s.sub[i])} substituted, ${fmt(s.na[i])} not available.</title>
      <rect class="hit" x="${pad + k * bw}" y="4" width="${bw}" height="${H - pad - 4}" rx="2"/>${rects}</g>`;
    line += `${k ? "L" : "M"}${pad + k * bw + bw / 2},${y(s.N[i])}`;
  });
  return svg(W, H, `Last ${days.length} days: courses the diagnoses called for, against what was dispensed`, `
    <line x1="${pad}" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="var(--rule)"/>
    ${bars}<path d="${line}" fill="none" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="4 3" pointer-events="none"/>
    <text x="${pad}" y="${H - 4}" font-size="10" fill="var(--muted)">day ${days[0]}</text>
    <text x="${W}" y="${H - 4}" font-size="10" fill="var(--muted)" text-anchor="end">day ${days[days.length - 1]}</text>`);
}

function forecastChart(s, fc) {
  const hist = s.use.slice(-30), W = 320, H = 100, pad = 18, n = hist.length + fc.mean.length;
  const top = Math.max(1, ...hist, ...fc.mean, ...fc.hi.filter((v) => v !== null));
  const x = (i) => pad + (i / (n - 1)) * (W - pad), y = (v) => H - pad - (v / top) * (H - pad * 1.5);
  const path = (vals, off) => vals.map((v, i) => `${i ? "L" : "M"}${x(i + off)},${y(v)}`).join("");
  const band = fc.lo[0] === null ? "" :
    `<path d="${path(fc.hi, hist.length)}L${fc.lo.map((v, i) => `${x(hist.length + fc.lo.length - 1 - i)},${y(fc.lo[fc.lo.length - 1 - i])}`).join("L")}Z" fill="var(--carbon)" opacity=".14"/>`;
  return svg(W, H, `Expected use over the last ${hist.length} days and the next ${fc.mean.length}`, `
    <line x1="${pad}" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="var(--rule)"/>
    <line x1="${x(hist.length - 0.5)}" x2="${x(hist.length - 0.5)}" y1="4" y2="${H - pad}" stroke="var(--rule)" stroke-dasharray="2 3"/>
    ${band}<path d="${path(hist, 0)}" fill="none" stroke="var(--ink)" stroke-width="2" stroke-linejoin="round"/>
    <path d="${path(fc.mean, hist.length)}" fill="none" stroke="var(--carbon)" stroke-width="2" stroke-dasharray="5 3"/>
    <text x="${pad}" y="${H - 4}" font-size="10" fill="var(--muted)">last 30 days</text>
    <text x="${W}" y="${H - 4}" font-size="10" fill="var(--muted)" text-anchor="end">next ${fc.mean.length} days</text>`);
}

/* ---------- detail panel ---------- */
function evidence(c, p) {   // why triage picked this level, from the warehouse ledger and the diagnoses
  const filled = c.fill === null ? "" : ` (${pct(c.fill)} of what it asked for in the last four weeks)`;
  return {
    LOCAL: `${p.wh} is being supplied by the state${filled} and demand is normal, so the problem is at this PHC.`,
    WAREHOUSE: `The state has stopped filling ${p.wh}'s indents for this medicine${filled}, while other warehouses in ${p.st} are still supplied.`,
    "STATE-PROCUREMENT": `The state has stopped filling this medicine's indents at two or more of ${p.st}'s warehouses.`,
    NATIONAL: "Warehouses in more than one state have stopped receiving this medicine. No single state can fix that.",
    "DEMAND-SURGE": `Diagnoses across ${p.st} for the conditions this medicine treats are ${pct(c.lift - 1)} above normal, while ${p.wh} is still being supplied. This is a demand surge, not a supply failure.`,
  }[c.level];
}

function tree(c, p) {
  if (c.level === "DEMAND-SURGE") return "";
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
  const beds = Array.from({ length: Math.min(b.capacity, 40) }, (_, i) => `<i class="${i < b.occupied ? "on" : ""}"></i>`).join("");
  const rows = fa.staff.map((r) => `<tr><th scope="row">${CADRE[r.cadre]}</th><td>${r.in_position} of ${r.sanctioned}</td>
    <td>${r.in_position ? (r.marked_present ? "Present" : "Absent") : "n/a"}</td><td>${atWork(r)}</td>
    ${r.true_present === undefined ? "" : `<td>${r.true_present ? "At work" : "Away"}</td>`}</tr>`).join("");
  return `<section><h3>${p.phc} today</h3>
    <div class="beds" aria-hidden="true">${beds}</div>
    <p class="why">Beds: <strong>${b.occupied} of ${b.capacity}</strong> occupied, counted from admissions and recorded discharges${b.pressure ? ". Every bed is taken" : ""}.${early}${truth}</p>
    <div class="scroll"><table class="data staff"><thead><tr><th scope="col">Staff</th><th scope="col">In post</th><th scope="col">Attendance</th><th scope="col">At work, from care records</th>${fa.staff[0].true_present === undefined ? "" : `<th scope="col">Ground truth</th>`}</tr></thead><tbody>${rows}</tbody></table></div>
    <p class="note">Staff are shown by role only. No individuals and no location tracking.</p></section>`;
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
    `<p class="truthline"><strong>Ground truth:</strong> ${fmt(c.true)} ${unit} on the shelf (${c.true_cover} days of use).</p>`;
  const alarm = !c.alarm ? `<h3>No alarm</h3><p class="why">Care at this PHC matches what the diagnoses call for, and deliveries minus dispensing leave more than ${state.meta.low} days of use.</p>` : `
    <h3>Alarm since day ${c.onset}</h3>
    ${c.by_stock ? `<p class="why">Care still looks normal, but deliveries minus dispensing leave only about ${c.shadow} days of use. The shelf is running down before anyone has started rationing.</p>` : ""}
    <p class="why">${evidence(c, p)}</p>
    ${tree(c, p)}
    <p class="action ${["LOCAL", "WAREHOUSE", "DEMAND-SURGE"].includes(c.level) ? "local" : ""}"><strong>Suggested next step:</strong> ${esc(state.meta.actions[c.level])}. Where it broke is read from the warehouse ledger; confirm with the district store before escalating.</p>`;
  const asked = c.confirmed ? `<p class="done">The pharmacist said: ${c.confirmed === "empty" ? "it's finished" : "we have it"}.</p>` : "";
  const panel = $("#detail");
  if (state.fresh) {
    panel.classList.remove("enter");
    void panel.offsetWidth;   // restart the entrance animation
    panel.classList.add("enter");
    panel.scrollTop = 0;
  } else panel.classList.remove("enter");
  state.fresh = false;
  panel.innerHTML = `
    <h2>${drugName(j)}</h2>
    <p class="where">${p.phc}, ${p.wh}, ${p.st}, day ${t}</p>
    <div class="versus">
      <div class="reg"><small>Register says</small><span class="ledger big${c.phantom ? " struck" : ""}">${fmt(c.book)}</span><small>${unit}, about ${c.cover} days of use</small></div>
      <div class="shelf is-${cls}"><small>Shelf, inferred from care</small><span class="big">${word}</span><span class="meter" aria-hidden="true"><i style="width:${sure}%"></i></span><small>${sure}% sure</small></div>
    </div>
    <p class="why">Deliveries in, minus what the dispensing slips took out: about <strong>${fmt(c.shadow)} days</strong> of use left${c.shadow < state.meta.low ? `, under the ${state.meta.low}-day warning line` : ""}.</p>
    ${truth}
    <section>
      <h3>What the care shows</h3>
      <p class="why">In the last 14 days the diagnoses here called for about ${fmt(sum("N"))} courses.
        ${fmt(sum("full"))} were given in full, ${fmt(sum("ration"))} were cut short, ${fmt(sum("sub"))} were switched to a guideline substitute and ${fmt(sum("na"))} were marked not available.</p>
      ${chart(s)}
      <p class="chart-key" aria-hidden="true">
        <span><i class="sw" style="background:var(--ok)"></i>Full course</span><span><i class="sw scarce"></i>Cut short</span>
        <span><i class="sw sub"></i>Substitute</span><span><i class="sw out"></i>Not available</span>
        <span><i class="dash"></i>Courses the diagnoses called for</span>
      </p>
    </section>
    <section>${alarm}</section>
    <section class="confirm">
      <h3>Ask the pharmacist</h3>
      <p class="why">Ask them to check the shelf. Their answer updates the estimate.</p>
      ${asked}
      <div class="buttons">
        <button type="button" class="yes" data-answer="empty">Yes, it's finished<span class="or" lang="or">ହଁ, ସରିଯାଇଛି</span></button>
        <button type="button" data-answer="available">No, we have it<span class="or" lang="or">ନା, ଅଛି</span></button>
        <button type="button" class="rec" aria-pressed="false">Record the answer<span class="or">Any language</span></button>
      </div>
      <p id="confirmMsg" role="status"></p>
    </section>
    <section>
      <h3>Next ${fc.days.length} days</h3>
      <p class="why">The diagnoses point to about <strong>${fmt(fc.total)}</strong> ${unit} of use. Dispensing history, with the stock-out days filled in, suggests about ${fmt(fc.consumption_total)}.</p>
      ${forecastChart(s, fc)}
    </section>
    ${facilityBlock(fa, p)}`;
  panel.querySelectorAll(".confirm button[data-answer]").forEach((b) => b.addEventListener("click", () => confirmShelf(b.dataset.answer)));
  panel.querySelector(".rec").addEventListener("click", (e) => recordAnswer(e.currentTarget));
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

/* ---------- lower sections ---------- */
function renderMoves(plan) {
  const sel = state.sel && state.meta.facilities[state.sel.f].id;
  const moves = [...plan.transfers].sort((a, b) => (b.to_fac === sel || b.from_fac === sel) - (a.to_fac === sel || a.from_fac === sel) || b.courses - a.courses);
  const end = (id) => { const p = place(facOf(id)); return `<span class="end">${p.phc}<small>${p.wh}</small></span>`; };
  const shown = moves.slice(0, 8).map((m) => {
    const j = drugOf(m.drug), mine = m.to_fac === sel || m.from_fac === sel;
    return `<li class="move${mine ? " mine" : ""}">
      <span class="qty"><span class="vh">Send</span><b>${m.courses}</b> courses<small>${fmt(m.units)} ${unitOf(j)}</small></span>
      <span class="med">${drugName(j)}</span>
      <span class="route"><span class="vh">from</span>${end(m.from_fac)}<span class="road">${m.minutes} min by road</span><span class="vh">to</span>${end(m.to_fac)}</span>
      <span class="st">${place(facOf(m.to_fac)).st}</span></li>`;
  }).join("");
  const more = moves.length > 8 ? `<p class="note">And ${moves.length - 8} smaller transfers. Each goes out as a DVDMS-style issue order.</p>` : "";
  const groups = {};
  for (const e of plan.escalations) {
    const k = `${e.drug}|${e.level}|${e.fac.split("-")[0]}`;
    groups[k] = (groups[k] || 0) + 1;
  }
  const escRows = Object.entries(groups).map(([k, n]) => {
    const [drug, level, st] = k.split("|"), nat = level === "NATIONAL";
    return `<li><span class="tag${nat ? " nat" : ""}">${nat ? "Likely national shortage" : "Likely state procurement gap"}</span>
      <span><strong>${drugName(drugOf(drug))}</strong> in ${stateName(st)}: ${n} ${n === 1 ? "PHC" : "PHCs"} short. ${esc(cap(state.meta.actions[level]))}.</span></li>`;
  }).join("");
  $("#moves").innerHTML = (shown ? `<ul class="moves">${shown}</ul>${more}` : `<p class="note">Nothing to move today. No PHC in a local shortage has a calm neighbour with stock to spare.</p>`)
    + (escRows ? `<h3>Escalate instead of moving stock</h3><ul class="plain esc">${escRows}</ul>` : "");
}

function renderNational(n) {
  const rows = n.exports.map((ex) => {
    const tot = (k) => ex.rows.reduce((a, r) => a + r[k], 0);
    const [ok, sc, out] = [tot("OK"), tot("SCARCE"), tot("OUT")];
    const seg = (v, cls) => (v ? `<i class="${cls}" style="flex-grow:${v}"></i>` : "");
    return `<tr><th scope="row">${stateName(ex.state)}</th>
      <td><span class="comp" aria-hidden="true">${seg(ok, "ok")}${seg(sc, "scarce")}${seg(out, "out")}</span></td>
      <td class="num">${fmt(ok)}</td><td class="num">${fmt(sc)}</td><td class="num">${fmt(out)}</td>
      <td class="num">${fmt(n.raw_rows[ex.state])} records</td><td class="num">${(n.export_bytes[ex.state] / 1024).toFixed(1)} KB of counts</td></tr>`;
  }).join("");
  const flags = Object.entries(n.view).flatMap(([drug, v]) => {
    const j = drugOf(drug), out = [];
    if (v.national) out.push(`<li><strong>${drugName(j)}</strong>: warehouses in ${v.short_states.map(stateName).join(" and ")} have stopped receiving it. Likely national supply failure. In testing this flag caught every injected national failure, about a month in and before most of the PHC stock-outs it caused.</li>`);
    else if (v.surge) out.push(`<li><strong>${drugName(j)}</strong>: demand is surging across states. Raise indents.</li>`);   // a supply break outranks a surge, as in triage
    return out;
  }).join("");
  const priors = Object.entries(n.priors).map(([drug, p]) =>
    `<tr><th scope="row">${drugName(drugOf(drug))}</th><td class="num">${pct(p.rho)}</td><td class="num">${pct(p.tau)}</td></tr>`).join("");
  $("#national").innerHTML = `<div class="scroll"><table class="data"><thead><tr><th scope="col">State</th><th scope="col">Share of PHC medicines</th><th scope="col" class="num">Stocked</th><th scope="col" class="num">Running short</th><th scope="col" class="num">Empty</th><th scope="col" class="num">Kept inside the state</th><th scope="col" class="num">Sent to the national view</th></tr></thead><tbody>${rows}</tbody></table></div>
    <h3>Patterns across states</h3>${flags ? `<ul class="plain">${flags}</ul>` : `<p class="note">No medicine shows a cross-state pattern today.</p>`}
    <details><summary>What states get back</summary>
      <p class="note">National medians a new state can start from instead of waiting a month: how closely prescribing follows the rulebook, and how far registers can be trusted. In testing, a state three days in had about 40% fewer false alarms with these than with its own three days, the same as a month of its own history.</p>
      <div class="scroll"><table class="data"><thead><tr><th scope="col">Medicine</th><th scope="col" class="num">Prescribing follows the rulebook</th><th scope="col" class="num">Register trust</th></tr></thead><tbody>${priors}</tbody></table></div>
    </details>`;
}

function proof(m) {
  const days = (n) => `${n} ${n === 1 ? "day" : "days"}`;
  const lead = (d) => d === null || Number.isNaN(d) ? "n/a" : d > 0 ? `${days(d)} before` : d < 0 ? `${days(-d)} after` : "same day";
  const meter = (v) => `<td class="pm"><span>${pct(v)}</span><i style="--v:${Math.round(100 * (v || 0))}%" aria-hidden="true"></i></td>`;
  const rows = [["Anumaan", m.model],
    [`The register, tuned to the same false-alarm budget (${m.register_best ? m.register_best.threshold : "?"} days)`, m.register_best],
    ["The register at 14 days, a common reorder level", m.register["14"]],
    ["Dispensing history only, no diagnoses", m.drug_only],
    ["Simple threshold on the same rulebook", m.crg_rule]].filter(([, r]) => r);
  $("#proof").innerHTML = `<thead><tr><th scope="col">Method</th><th scope="col">Stock-outs of 4+ days caught</th><th scope="col">Warned before the shelf emptied</th><th scope="col">Typical warning</th><th scope="col" class="num">False alarms per medicine per year</th></tr></thead>
    <tbody>${rows.map(([n, r], k) => `<tr${k ? "" : ' class="lead"'}><th scope="row">${n}</th>${meter(r.recall_4d)}${meter(r.early)}<td>${lead(r.median_lead)}</td><td class="num">${r.false_per_series_year.toFixed(2)}</td></tr>`).join("")}</tbody>`;
  $("#triageNote").textContent = `Where it broke is read from the warehouse ledger: right for ${pct(m.triage_7d.accuracy)} of alarms a week after they start, against ${pct(m.triage_majority)} for always guessing the commonest cause. Early warning comes from staff rationing and from deliveries minus dispensing; where staff never ration, Anumaan still warns before the shelf empties for more than half of outages.`;
}

async function init() {
  state.meta = await get("/api/meta");
  $("#day").max = state.meta.days - 1;
  $("#dayMax").textContent = state.meta.days - 1;
  $("#grammarNote").textContent = `Rulebook ${state.meta.grammar}: ${state.meta.grammar_note}`;
  buildGrid();
  proof(await get("/api/eval"));
  const s = state.meta.start;
  state.sel = { f: s.f, j: s.j };
  state.fresh = true;
  await show(s.day);
  setTimeout(() => document.body.classList.remove("intro"), 1800);
}

let pending;
$("#day").addEventListener("input", (e) => {
  const t = +e.target.value;
  setDay(t);
  clearTimeout(pending);
  pending = setTimeout(() => show(t), 60);
});
$("#truth").addEventListener("change", (e) => { state.truth = e.target.checked; show(state.day); });
$("#play").addEventListener("click", () => {
  const b = $("#play"), label = b.querySelector("span");
  if (state.timer) { clearInterval(state.timer); state.timer = null; label.textContent = "Play"; b.setAttribute("aria-pressed", "false"); return; }
  label.textContent = "Pause"; b.setAttribute("aria-pressed", "true");
  state.timer = setInterval(() => {
    if (state.day >= state.meta.days - 1) return $("#play").click();
    show(state.day + 1);
  }, 700);
});
init().catch((e) => {
  document.body.classList.remove("intro");
  $("#detail").innerHTML = `<p class="hint">The demo could not start: ${esc(e.message)}. Check that the server is running.</p>`;
});
