const $ = (s) => document.querySelector(s);
const NETS = ["demo", "kerala", "live"];
const asked = new URLSearchParams(location.search).get("net");
const state = { net: NETS.includes(asked) || asked?.startsWith("whatif:") ? asked : "demo", meta: null, day: 0, truth: false, sel: null, timer: null, rec: null,
  cache: new Map(), side: new Map(), dayData: null, cells: [], byKey: new Map(), prevPhantom: null, prevDay: null, fresh: false };
const REGIME = { OK: ["ok", "Stocked"], SCARCE: ["scarce", "Running short"], OUT: ["out", "Empty"] };
const WORD = { tab: "tablets", cap: "capsules", sachet: "sachets" };
const CADRE = { MO: "Medical officer", SN: "Staff nurse", PH: "Pharmacist", LT: "Lab technician" };
// the chips under the title -> the /api/google rows each stands for; OR-Tools is a library in the app, so it has none
const STACK = [["Gemini", ["Gemini on Vertex AI", "Gemini grammar compiler"]],
  ["Vertex AI", ["Gemini on Vertex AI", "Text-to-Speech (Gemini-TTS on Vertex AI)"]],
  ["BigQuery", ["BigQuery clean room", "TimesFM on BigQuery (AI.FORECAST)"]], ["Maps Routes", ["Google Maps Routes API"]],
  ["OR-Tools", []], ["Pub/Sub", ["Pub/Sub"]], ["Cloud Run", ["Cloud Run"]], ["Text-to-Speech", ["Text-to-Speech (Gemini-TTS on Vertex AI)"]]];
const LIVENESS = ["live", "in the app", "configured", "cached", "offline", "off", "not configured"];   // a chip shows its best row
const fmt = (n) => Math.round(n).toLocaleString("en-IN");
const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
const pct = (x) => (x === null || x === undefined || Number.isNaN(x) ? "n/a" : `${Math.round(100 * x)}%`);
const cap = (s) => s[0].toUpperCase() + s.slice(1);
const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
Object.assign(state, { page: 0, display: "list", detailTab: "evidence", request: 0, detailRequest: 0, saving: false });
const PAGE_SIZE = 8;
let toastTimer;
function tween(el, to) {   // counts glide to their new value, so a change of day reads as change
  const from = +el.dataset.v || 0;
  el.dataset.v = to;
  if (calm() || from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = from ? 350 : 700;
  const step = (now) => {
    if (+el.dataset.v !== to) return;   // a newer value took over
    const k = Math.min(1, (now - t0) / dur);
    el.textContent = fmt(from + (to - from) * (1 - (1 - k) ** 3));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function notify(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 5500);
}
const playLabel = (on) => (state.meta?.live ? (on ? "Pause feed" : "Start feed") : on ? "Pause" : "Play");
function stopReplay() {
  clearTimeout(state.timer);
  state.timer = null;
  $("#play span").textContent = playLabel(false);
  $("#play").setAttribute("aria-pressed", "false");
}
function showError(message) {
  stopReplay();
  $("#loadError span").textContent = message;
  $("#loadError").hidden = false;
}
function renderSignals() {
  if (!state.dayData) return;
  const query = $("#search").value.trim().toLowerCase().replace(/\s+/g, " ");
  const region = $("#stateFilter").value, status = $("#statusFilter").value;
  const matches = (c) => {
    const p = place(c.f), words = `${p.st} ${p.wh} ${p.phc} ${drugName(c.j)} ${state.meta.facilities[c.f].id}`.replace(/\s+/g, " ").toLowerCase();
    return (!query || words.includes(query)) && (!region || [state.meta.facilities[c.f].st, state.meta.facilities[c.f].wh].includes(region))
      && (!status || (status === "phantom" ? c.phantom : c.regime === status));
  };
  const rank = (c) => c.phantom ? 0 : c.regime === "OUT" ? 1 : c.regime === "SCARCE" ? 2 : 3;
  const rows = state.dayData.cells.filter(matches).sort((a, b) => rank(a) - rank(b) || a.f - b.f || a.j - b.j);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  const visible = rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  $("#signalList").classList.toggle("refresh", Boolean(state.animateList) && !calm());
  state.animateList = false;
  $("#signalList").innerHTML = rows.length ? `<div class="scroll"><table class="signal-table"><thead><tr><th scope="col">Facility and medicine</th><th scope="col">Shelf</th><th scope="col" class="register-col num">Register</th><th scope="col" class="num">Days left</th></tr></thead><tbody>${visible.map((c, i) => {
    const p = place(c.f), [cls, word] = REGIME[c.regime], selected = state.sel?.f === c.f && state.sel?.j === c.j;
    return `<tr class="${selected ? "selected" : ""}" data-f="${c.f}" data-j="${c.j}" style="--i:${i}"><td><button class="signal-link" data-f="${c.f}" data-j="${c.j}" aria-pressed="${selected}" aria-label="Investigate ${p.phc}, ${p.wh}, ${p.st}, ${drugName(c.j)}">${drugName(c.j)}</button><small>${p.phc}, ${p.wh}, ${p.st}${c.as_of === undefined ? "" : `. Last report: day ${c.as_of}`}</small></td><td><span class="badge ${cls}${c.phantom ? " phantom" : ""}">${c.phantom ? "Hidden stock-out" : word}</span>${c.confirmed ? '<small>Shelf check recorded</small>' : ''}</td><td class="register-col num"><span class="ledger${c.phantom ? " struck" : ""}">${fmt(c.book)}</span><small>${unitOf(c.j)}</small></td><td class="num">${c.shadow === null ? '<small>No use yet</small>' : `<span class="days">${fmt(c.shadow)}</span><span class="cover ${cls}" style="--v:${Math.min(100, Math.round((100 * c.shadow) / 30))}%" aria-hidden="true"></span>`}${c.true === undefined ? '' : `<small>Actual: ${units(c.true, c.j)}</small>`}</td></tr>`;
  }).join("")}</tbody></table></div>` : `<div class="empty-state"><strong>No matching signals</strong><p>Try another medicine, facility, or availability filter.</p><button class="button" id="clearFilters">Clear filters</button></div>`;
  document.querySelectorAll(".metric").forEach((m) => m.setAttribute("aria-pressed", String(m.dataset.status === status)));
  $("#signalList").setAttribute("aria-busy", "false");
  $("#signalList").hidden = state.display !== "list";
  $("#grid").hidden = state.display !== "matrix";
  $("#pagination").hidden = state.display !== "list" || !rows.length;
  $("#pageNumber").textContent = `${state.page + 1} / ${pages}`;
  $("#prevPage").disabled = state.page === 0;
  $("#nextPage").disabled = state.page >= pages - 1;
  $("#resultCount").textContent = state.display === "list" && rows.length ? `${state.page * PAGE_SIZE + 1}-${Math.min(rows.length, (state.page + 1) * PAGE_SIZE)} of ${rows.length} signals` : `${rows.length} matching signals`;
  state.cells.forEach((b, key) => { b.disabled = !matches(state.byKey.get(key)); });
}

const POST = '<span class="tag post">Added after the 30 Sep submission</span>';   // as index.html tags it
const VIEWS = {
  command: ["Command center", "District command center", "The register says it’s on the shelf. The care says otherwise. Anumaan finds those gaps, district by district."],
  overview: ["Stock signals", "Network overview", "The register says it’s on the shelf. The care says otherwise."],
  transfers: ["Redistribution", "Put supply where it’s needed", "Prioritize transfers and escalate the gaps that need a wider response."],
  care: ["Beds & staff", "Beds and staff", "Where a bed is free, and which attendance marks the care record does not back."],
  "national-view": ["National view", "See the bigger picture", "Connect state-level signals while keeping care records local."],
  validation: ["Performance", "Confidence, backed by evidence", "What the model catches, how early, and where it falls short."]
};
function navigate() {
  let key = location.hash.slice(1) || "command";
  if (key === "guide") { $("#guide").showModal(); return; }
  if (!VIEWS[key]) key = "command";
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== key; });
  document.querySelectorAll("[data-view]").forEach((a) => { if (a.dataset.view === key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  const [label, title, description] = VIEWS[key];
  $("#pageTitle").innerHTML = key === "command" ? `${title} ${POST}` : title;
  $("#pageDescription").textContent = description;
  document.title = `${label} | Anumaan`;
  hideTip();
  if (key === "command" && mapArgs) renderMap(...mapArgs);   // drawn while hidden, it had no width to fit
}

function medName(id) {   // amoxicillin_500 -> Amoxicillin 500. Names come from the (Gemini-compiled) rulebook, so escape them
  const [name, strength] = id.split(/_(?=[^_]+$)/);
  const base = name.split("_").map((w) => (w.length <= 3 ? w.toUpperCase() : w)).join(" ");   // ifa -> IFA, ors -> ORS
  return esc((base[0].toUpperCase() + base.slice(1)) + (strength && /^\d/.test(strength) ? ` ${strength}` : ""));
}
const drugName = (j) => medName(state.meta.drugs[j].name);
const drugOf = (name) => state.meta.drugs.findIndex((d) => d.name === name);
const unitOf = (j) => WORD[state.meta.drugs[j].unit] || "units";
const units = (n, j) => plural(n, unitOf(j).slice(0, -1), unitOf(j));   // 1 tablet, 2 tablets
function place(f) {   // a scenario names its real state and districts; the demo network is numbered
  const fac = state.meta.facilities[f], [, w, p] = fac.id.split("-"), named = state.meta.names.warehouses?.[fac.wh];
  return { st: stateName(fac.st), wh: named ? esc(named) : `Warehouse\u00a0${String.fromCharCode(65 + +w.slice(1))}`, phc: `PHC\u00a0${+p.slice(1) + 1}` };   // never split "PHC 6" across lines
}
const facOf = (id) => state.meta.facilities.findIndex((x) => x.id === id);
const stateName = (id) => (state.meta.names.states?.[id] ? esc(state.meta.names.states[id]) : `State\u00a0${+id.slice(1) + 1}`);
const api = (path) => `${path}${path.includes("?") ? "&" : "?"}net=${encodeURIComponent(state.net)}`;   // every call names its network
const road = (minutes) => (state.meta.real_roads ? `${minutes} min by road` : `about ${minutes} min by road`);
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw Object.assign(new Error(`${url} answered ${r.status}`), { status: r.status });
  return r.json();
}
async function post(url, body) {   // errors carry .status and the server's reason, when it gave one in words
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(typeof out.detail === "string" ? out.detail : `${url} answered ${r.status}`), { status: r.status });
  return out;
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
  state.cells = [];
  grid.querySelectorAll(".cell").forEach((b) => { state.cells[+b.dataset.f * drugs.length + +b.dataset.j] = b; });
  if (grid.dataset.bound) return;
  grid.dataset.bound = "true";
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
    b.setAttribute("aria-pressed", String(!!sel));
    b.innerHTML = c.true === undefined ? "" :
      `<span class="truthdot ${c.true_cover < 0.5 ? "t-out" : c.true_cover < 7 ? "t-low" : "t-ok"}" aria-hidden="true"></span>`;
  }
  state.prevPhantom = phantoms;
  state.prevDay = data.day;
  count.out += count.phantom;
  for (const [k, n] of Object.entries(count)) tween($(`#legend [data-k="${k}"]`), n);
  document.querySelectorAll(".st-count").forEach((el) => {
    const n = perState[el.dataset.st] || 0;
    el.textContent = n ? `${n} hidden ${n === 1 ? "stock-out" : "stock-outs"}` : "Registers agree";
    el.classList.toggle("has", n > 0);
  });
  renderSignals();
}

const tip = $("#tip");
function mark(b, on) {
  b.closest("table").querySelector(`th.drug[data-j="${b.dataset.j}"]`).classList.toggle("hl", on);
  b.closest("tr").querySelector("th.phc").classList.toggle("hl", on);
}
function showTip(b) {
  const c = state.byKey.get(+b.dataset.f * state.meta.drugs.length + +b.dataset.j);
  if (!c) return;
  const p = place(c.f), [, word] = REGIME[c.regime];
  tip.innerHTML = `<strong>${drugName(c.j)}</strong><span>${p.phc}, ${p.wh}</span>
    <span>Register: ${units(c.book, c.j)}</span><span${c.phantom ? ' class="alert"' : ""}>Shelf: ${word.toLowerCase()}${c.phantom ? ", register disagrees" : ""}</span>
    ${c.true === undefined ? "" : `<span>Actual: ${units(c.true, c.j)}</span>`}`;
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
    ? `${n} ${n === 1 ? "shelf looks" : "shelves look"} empty in care records, while the register still shows stock.`
    : "Every register agrees with the shelf today.";
  const line = $("#phantomLine");
  line.classList.toggle("draw", draw);
  line.innerHTML = html;
}

function setDay(t) {   // the thumb and fill glide to the new day; the counter rolls to it
  $("#day").value = t;
  tween($("#dayOut"), t);
  $(".range").style.setProperty("--t", t / Math.max(1, state.meta.clock));
}
function fading(el, on) {   // keep what is on screen while the next day loads, instead of flashing a skeleton
  if (on && !el.children.length) el.innerHTML = '<div class="skeleton" role="status" aria-label="Loading"></div>';
  else el.classList.toggle("updating", on);
}

async function show(t) {
  const request = ++state.request, truth = state.truth;
  state.day = t;
  setDay(t);
  $("#signalList").setAttribute("aria-busy", "true");
  $("#detail").inert = true;
  const rails = ["#moves", "#careBoard", "#national", "#ccBody", "#ccMap"].map($);
  rails.forEach((el) => fading(el, true));
  try {
  const data = await cached(state.cache, `${t}|${truth}`, api(`/api/day/${t}${truth ? "?truth=true" : ""}`));
  if (state.request !== request) return;
  state.dayData = data;
  state.dayTruth = truth;
  $("#loadError").hidden = true;
  paintGrid(data);
  headline(data, state.fresh);
  verdict(data);
  const detail = state.sel ? renderDetail() : Promise.resolve();
  const [plan, board, nat, net] = await Promise.all([cached(state.side, `plan|${t}`, api(`/api/plan?t=${t}`)),
    cached(state.side, `care|${t}|${truth}`, api(`/api/care?t=${t}${truth ? "&truth=true" : ""}`)),
    cached(state.side, `nat|${t}`, api(`/api/national?t=${t}`)), cached(state.side, `district|${t}`, api(`/api/district?t=${t}`))]);
  if (state.request !== request) return;
  const dist = await districtFor(t, net);
  if (state.request !== request) return;
  renderMoves(plan);
  renderCare(board);
  renderNational(nat);
  renderCommand(net, dist, plan, board);
  rails.forEach((el) => fading(el, false));
  await detail;
  $("#detail").inert = false;
  } catch (e) {
    if (state.request !== request) return;
    $("#signalList").setAttribute("aria-busy", "false");
    if (state.dayData) {
      state.day = state.dayData.day;
      state.truth = state.dayTruth;
      $("#truth").checked = state.truth;
      setDay(state.day);
      await renderDetail();
    }
    $("#detail").inert = false;
    rails.forEach((el) => fading(el, false));
    $("#moves").innerHTML = '<p class="note">Transfer recommendations are unavailable. Use Try again above to reload.</p>';
    $("#careBoard").innerHTML = '<p class="note">Beds and staff are unavailable. Use Try again above to reload.</p>';
    $("#national").innerHTML = '<p class="note">The national summary is unavailable. Use Try again above to reload.</p>';
    $("#ccBody").innerHTML = '<p class="note cc-note">The district screen is unavailable. Use Try again above to reload.</p>';
    showError("We couldn’t load this replay day. Check your connection and try again.");
  }
}

function select(f, j, byUser = false) {
  if (state.dayData?.day !== state.day) { notify("Loading this replay day. Please wait."); return; }
  if (state.saving || state.rec) { notify("Finish the shelf check before changing facilities."); return; }
  stopReplay();
  state.sel = { f, j };
  state.fresh = true;
  paintGrid(state.dayData);
  headline(state.dayData, true);
  renderDetail();
  const plan = state.side.get(`plan|${state.day}`);
  if (plan) renderMoves(plan);
  if (byUser && matchMedia("(max-width: 1050px)").matches) {
    $("#detail").focus({ preventScroll: true });
    $("#detail").scrollIntoView({ behavior: calm() ? "auto" : "smooth", block: "start" });
  }
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
  const where = c.lift < 1.2 && c.district_lift >= 2 ? [`in the district served by ${p.wh}`, c.district_lift] : [`across ${p.st}`, c.lift];   // a surge in one district only
  return {
    LOCAL: `${p.wh} is being supplied by the state${filled} and demand is normal, so the problem is at this PHC.`,
    WAREHOUSE: `The state has stopped filling ${p.wh}'s indents for this medicine${filled}, while other warehouses in ${p.st} are still supplied.`,
    "STATE-PROCUREMENT": `The state has stopped filling this medicine's indents at ${c.starved} of ${p.st}'s ${c.warehouses} warehouses. One warehouse failing on its own would not look like this.`,
    NATIONAL: "Warehouses in more than one state have stopped receiving this medicine. No single state can fix that.",
    "DEMAND-SURGE": `Diagnoses ${where[0]} for the conditions this medicine treats are ${pct(where[1] - 1)} above normal, while ${p.wh} is still being supplied. This is a demand surge, not a supply failure.`,
  }[c.level];
}

function tree(c, p) {
  if (c.level === "DEMAND-SURGE") return "";
  const levels = ["LOCAL", "WAREHOUSE", "STATE-PROCUREMENT", "NATIONAL"];
  const names = [`This PHC (${p.phc})`, p.wh, `${p.st} medical services corporation`, "National supply"];
  const s = state.meta.scenario, scripted = s ? `<em class="scripted">A scripted failure in this ${s.whatif ? "what-if" : "replay"}, not real data</em>` : "", at = levels.indexOf(c.level);
  return `<ol class="tree">${names.map((nm, i) => `<li class="${i < at ? "hit" : i === at ? "broke" : ""}">${nm}${i === at ? ` is where it broke${scripted}` : ""}</li>`).join("")}</ol>`;
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
  const truth = b.true_occupied === undefined ? "" : ` <strong>Actual:</strong> ${b.true_occupied} occupied.`;
  const beds = Array.from({ length: Math.min(b.capacity, 40) }, (_, i) => `<i class="${i < b.occupied ? "on" : ""}"></i>`).join("");
  // four narrow columns so the table fits the side panel without scrolling: posts sit under the role
  const rows = fa.staff.map((r) => `<tr><th scope="row">${CADRE[r.cadre]}<small>${r.in_position} of ${r.sanctioned} in post</small></th>
    <td>${r.in_position ? (r.marked_present ? "Present" : "Absent") : "n/a"}</td><td>${atWork(r)}</td>
    ${r.true_present === undefined ? "" : `<td>${r.true_present ? "At work" : "Away"}</td>`}</tr>`).join("");
  return `<section><h3>${p.phc} today</h3>
    <div class="beds" aria-hidden="true">${beds}</div>
    <p class="why">Beds: <strong>${b.occupied} of ${b.capacity}</strong> occupied, counted from admissions and recorded discharges${b.pressure ? ". Every bed is taken" : ""}.${early}${truth}</p>
    <table class="data staff"><thead><tr><th scope="col">Staff</th><th scope="col">Attendance</th><th scope="col">At work, from care records</th>${fa.staff[0].true_present === undefined ? "" : `<th scope="col">Actual</th>`}</tr></thead><tbody>${rows}</tbody></table>
    <p class="note">Staff are shown by role only. No individuals and no location tracking.</p></section>`;
}

async function renderDetail() {
  const request = ++state.detailRequest, truthMode = state.truth;
  const { f, j } = state.sel, t = state.day;
  const panel = $("#detail");
  panel.setAttribute("aria-busy", "true");
  if (state.fresh || !panel.querySelector(".detail-tabs"))
    panel.innerHTML = '<div class="panel-heading"><h2>Loading investigation…</h2></div><div class="detail-content"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>';
  else panel.classList.add("updating");   // same signal, new day: keep it on screen
  const c = state.dayData.cells.find((x) => x.f === f && x.j === j);
  const tq = state.truth ? "&truth=true" : "";
  try {
  const [s, fc, fa] = await Promise.all([get(api(`/api/series/${f}/${j}?t=${t}${tq}`)),
    get(api(`/api/forecast/${f}/${j}?t=${t}`)), get(api(`/api/facility/${f}?t=${t}${tq}`))]);
  if (state.detailRequest !== request || state.day !== t || state.truth !== truthMode || state.sel.f !== f || state.sel.j !== j) return;
  const p = place(f), [cls, word] = REGIME[c.regime], unit = unitOf(j);
  const sum = (k) => s[k].slice(-14).reduce((a, b) => a + b, 0);
  const sure = Math.min(99, Math.round(100 * Math.max(...c.p)));   // never claim certainty
  const lang = state.meta.languages[state.briefLang] ? state.briefLang : "en";
  const truth = c.true === undefined ? "" :
    `<p class="truthline"><strong>Actual, from the simulator:</strong> ${units(c.true, j)} on the shelf (${c.true_cover} days of use).</p>`;
  const alarm = !c.alarm ? `<h3>No alarm</h3><p class="why">Care at this PHC matches what the diagnoses call for, and deliveries minus dispensing leave more than ${state.meta.low} days of use.</p>` : `
    <h3>Alarm since day ${c.onset}</h3>
    ${c.by_stock ? `<p class="why">Care still looks normal, but deliveries minus dispensing leave only about ${c.shadow} days of use. The shelf is running down before anyone has started rationing.</p>` : ""}
    <p class="why">${evidence(c, p)}</p>
    ${tree(c, p)}
    <p class="action ${["LOCAL", "WAREHOUSE", "DEMAND-SURGE"].includes(c.level) ? "local" : ""}"><strong>Suggested next step:</strong> ${esc(state.meta.actions[c.level])}. Where it broke is read from the warehouse ledger; confirm with the district store before escalating.</p>
    <h3 class="brief-title">Brief for the district officer <span class="tag">Machine-written</span></h3>
    <p class="why" id="briefNote">${briefNote(lang)}</p>
    <p class="brief-ask"><select id="briefLang" aria-label="Language of the brief">${languageOptions(lang)}</select>
      <button type="button" class="button" id="briefBtn">Write brief</button>
      <button type="button" class="button" id="listen" hidden><svg aria-hidden="true"><use href="#i-play"/></svg>Listen</button></p>
    <div id="briefOut" role="status"></div><p id="briefSay" class="note" role="status"></p><audio id="briefAudio" controls hidden></audio>`;
  const asked = c.confirmed ? `<p class="done">The pharmacist said: ${c.confirmed === "empty" ? "it's finished" : "we have it"}.</p>` : "";
  if (state.fresh) {
    panel.classList.remove("enter");
    void panel.offsetWidth;   // restart the entrance animation
    panel.classList.add("enter");
    panel.scrollTop = 0;
  } else panel.classList.remove("enter");
  state.fresh = false;
  panel.innerHTML = `
    <div class="panel-heading"><span class="detail-label">Selected signal</span><span class="badge ${cls}">${c.phantom ? "Hidden stock-out" : word}</span></div><div class="detail-content">
    <h2>${drugName(j)}</h2>
    <p class="where">${p.phc}, ${p.wh}, ${p.st}, day ${t}</p>
    <div class="versus">
      <div class="reg"><small>Register says</small><span class="ledger big${c.phantom ? " struck" : ""}">${fmt(c.book)}</span><small>${unit}, ${c.cover === null ? "no use yet" : `${c.cover} days of use`}</small></div>
      <div class="shelf is-${cls}"><small>Shelf, inferred from care</small><span class="big">${word}</span><span class="meter" aria-hidden="true"><i style="width:${sure}%"></i></span><small>${sure}% sure</small></div>
    </div>
    <p class="why">${c.shadow === null ? "No diagnosis here has called for this medicine yet, so there is no rate of use to count days against." : `Deliveries in, minus what the dispensing slips took out: about <strong>${fmt(c.shadow)} days</strong> of use left${c.shadow < state.meta.low ? `, under the ${state.meta.low}-day warning line` : ""}.`}</p>
    ${truth}
    <section class="verdict-block">${alarm}</section>
    <div class="detail-tabs" aria-label="Investigation sections"><button data-detail-tab="evidence">Evidence</button><button data-detail-tab="forecast">Forecast</button><button data-detail-tab="facility">Beds & staff</button></div>
    <section data-pane="evidence">
      <h3>What the care shows</h3>
      <p class="why">In the last 14 days the diagnoses here called for about ${fmt(sum("N"))} courses.
        ${fmt(sum("full"))} were given in full, ${fmt(sum("ration"))} were cut short, ${fmt(sum("sub"))} were switched to a guideline substitute and ${fmt(sum("na"))} were marked not available.</p>
      ${chart(s)}
      <p class="chart-key" aria-hidden="true">
        <span><i class="sw full"></i>Full course</span><span><i class="sw scarce"></i>Cut short</span>
        <span><i class="sw sub"></i>Substitute</span><span><i class="sw out"></i>Not available</span>
        <span><i class="dash"></i>Courses the diagnoses called for</span>
      </p>
    </section>
    <section class="confirm" data-pane="evidence">
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
    <section data-pane="forecast">
      <h3>Next ${fc.days.length} days</h3>
      <p class="why">The diagnoses point to about <strong>${fmt(fc.total)}</strong> ${unit} of use. Dispensing history, with the stock-out days filled in, suggests about ${fmt(fc.consumption_total)}.</p>
      ${forecastChart(s, fc)}
    </section>
    ${facilityBlock(fa, p).replace('<section>', '<section data-pane="facility">')}</div>`;
  panel.setAttribute("aria-busy", "false");
  panel.classList.remove("updating");
  setDetailTab(state.detailTab);
  panel.querySelectorAll("[data-detail-tab]").forEach((b) => b.addEventListener("click", () => setDetailTab(b.dataset.detailTab)));
  panel.querySelectorAll(".confirm button[data-answer]").forEach((b) => b.addEventListener("click", () => confirmShelf(b.dataset.answer)));
  panel.querySelector(".rec").addEventListener("click", (e) => recordAnswer(e.currentTarget));
  panel.querySelector("#briefBtn")?.addEventListener("click", writeBrief);
  panel.querySelector("#listen")?.addEventListener("click", listenBrief);
  panel.querySelector("#briefLang")?.addEventListener("change", (e) => { state.briefLang = e.target.value; $("#briefNote").textContent = briefNote(e.target.value); });
  } catch (e) {
    if (state.detailRequest !== request) return;
    panel.setAttribute("aria-busy", "false");
    panel.classList.remove("updating");
    panel.innerHTML = '<div class="empty-state"><strong>Investigation unavailable</strong><p>We couldn’t load the care evidence. Check your connection and try again.</p><button class="button" id="retryDetail">Retry investigation</button></div>';
    $("#retryDetail").addEventListener("click", renderDetail);
  }
}

/* ---------- the brief: written by Gemini, checked, and read aloud by Gemini-TTS ---------- */
function languageOptions(lang) {   // /api/meta's languages, grouped by whether a Google voice can read them
  const groups = [["GA", "Written and read aloud"], ["Preview", "Written, read aloud by a Preview voice"], [null, "Written only: no Google voice yet"]];
  return groups.map(([stage, label]) => {
    const xs = Object.entries(state.meta.languages).filter(([, v]) => v.voice_stage === stage);
    return xs.length ? `<optgroup label="${label}">${xs.map(([k, v]) => `<option value="${esc(k)}" lang="${esc(k)}"${k === lang ? " selected" : ""}>${esc(k === "en" ? v.name : `${v.native} (${v.name})`)}</option>`).join("")}</optgroup>` : "";
  }).join("");
}
function briefNote(lang) {
  const v = state.meta.languages[lang];
  const aloud = !v.voice ? "No Google voice reads it aloud yet." : !canSpeak(lang) ? "Reading aloud is not set up on this server."
    : `Listen reads it aloud${v.voice_stage === "Preview" ? " with a Preview voice" : ""}.`;
  return `Gemini writes it from the evidence above${lang === "en" ? "" : `, in ${v.name}; not reviewed by a native speaker`}. ${aloud}`;
}
const ttsRow = () => state.google?.services.find((r) => r.service.startsWith("Text-to-Speech"));
const canSpeak = (lang) => Boolean(state.meta.languages[lang]?.voice) && (!state.google || (Boolean(ttsRow()) && ttsRow().status !== "not configured"));

function setDetailTab(tab) {
  state.detailTab = tab;
  document.querySelectorAll("[data-detail-tab]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.detailTab === tab)));
  document.querySelectorAll("[data-pane]").forEach((section) => { section.hidden = section.dataset.pane !== tab; });
}

function lockCheck(locked) {
  state.saving = locked;
  stopReplay();
  clearTimeout(pending);
  ["#day", "#truth", "#play"].forEach((s) => { $(s).disabled = locked; });
  document.querySelectorAll(".confirm button").forEach((b) => { b.disabled = locked; });
}

function refreshAfterAnswer() {
  state.cache.clear();
  state.side.clear();
  return show(state.day);
}

async function confirmShelf(answer) {
  if (state.saving) return;
  lockCheck(true);
  const { f, j } = state.sel;
  $("#confirmMsg").textContent = "Saving shelf check…";
  try {
    const r = await fetch(api("/api/confirm"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ f, j, t: state.day, answer }) });
    if (!r.ok) throw new Error(`the server answered ${r.status}`);
    await refreshAfterAnswer();
    notify("Shelf check saved. The estimate now includes it.");
    if ($("#confirmMsg")) $("#confirmMsg").innerHTML = `<span class="done">Answer saved.</span> The estimate now includes it.`;
  } catch (e) {
    notify("Could not save the shelf check. Please try again.");
    if ($("#confirmMsg")) $("#confirmMsg").textContent = "Could not save the answer. Check your connection and try again.";
  } finally { lockCheck(false); }
}

async function recordAnswer(btn) {
  const say = (message) => { if ($("#confirmMsg")) $("#confirmMsg").textContent = message; };
  if (state.rec) { if (state.rec.state === "recording") state.rec.stop(); return; }
  if (state.saving) return;
  lockCheck(true);
  const { f, j } = state.sel, t = state.day;
  let stream, rec;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    rec = new MediaRecorder(stream);
  } catch {
    stream?.getTracks().forEach((track) => track.stop());
    lockCheck(false);
    return say("Microphone unavailable. Allow microphone access, or use the shelf-check buttons.");
  }
  const chunks = [];
  rec.ondataavailable = (e) => chunks.push(e.data);
  rec.onstop = async () => {
    clearTimeout(limit);
    stream.getTracks().forEach((track) => track.stop());
    state.rec = null;
    btn.disabled = true;
    btn.setAttribute("aria-pressed", "false");
    btn.firstChild.textContent = "Processing answer…";
    say("Listening to the answer…");
    try {
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      const r = await fetch(api(`/api/voice?f=${f}&j=${j}&t=${t}`), { method: "POST", headers: { "Content-Type": blob.type }, body: blob });
      if (r.status === 503) return say("Voice is unavailable on this demo. Use the shelf-check buttons.");
      if (r.status === 429) return say("Voice answers are used up for today. Use the shelf-check buttons.");
      if (!r.ok) return say("The recording could not be used. Record again, or use the buttons.");
      const h = await r.json();
      if (h.answer === "unclear") return say(`Heard: "${h.transcript_en}". The answer was not clear. Ask again, or use the buttons.`);
      await refreshAfterAnswer();
      say(`Heard: "${h.transcript_en}". Saved as ${h.answer === "empty" ? "finished" : "in stock"}. Likely reason: ${h.cause.replace(/_/g, " ")}.`);
      notify("Voice shelf check saved. The estimate has been updated.");
    } catch {
      say("Could not send the recording. Check your connection and try again.");
    } finally {
      lockCheck(false);
      btn.firstChild.textContent = "Record the answer";
    }
  };
  rec.onerror = () => { say("Recording interrupted. Please try again or use the buttons."); rec.stop(); };
  rec.start();
  state.rec = rec;
  btn.disabled = false;
  btn.setAttribute("aria-pressed", "true");
  btn.firstChild.textContent = "Stop and send";
  say("Recording. Ask whether the medicine is finished, then press Stop and send. Recording stops after 30 seconds.");
  const limit = setTimeout(() => { if (rec.state === "recording") rec.stop(); }, 30000);
}

/* ---------- lower sections ---------- */
function renderMoves(plan) {
  $("#moveCount").textContent = plan.transfers.length;
  const sel = state.sel && state.meta.facilities[state.sel.f].id;
  const moves = [...plan.transfers].sort((a, b) => (b.to_fac === sel || b.from_fac === sel) - (a.to_fac === sel || a.from_fac === sel) || b.courses - a.courses);
  const end = (id) => { const p = place(facOf(id)); return `<span class="end">${p.phc}<small>${p.wh}</small></span>`; };
  const shown = moves.slice(0, 8).map((m) => {
    const j = drugOf(m.drug), mine = m.to_fac === sel || m.from_fac === sel;
    return `<li class="move${mine ? " mine" : ""}">
      <span class="qty"><span class="vh">Send</span><b>${m.courses}</b> courses<small>${units(m.units, j)}</small></span>
      <span class="med">${drugName(j)}</span>
      <span class="route"><span class="vh">from</span>${end(m.from_fac)}<span class="road">${road(m.minutes)}</span><span class="vh">to</span>${end(m.to_fac)}</span>
      <span class="st">${place(facOf(m.to_fac)).st}${m.approved ? '<small class="done">Order approved</small>'
        : `<button type="button" class="button" data-approve="${esc(JSON.stringify({ t: state.day, net: state.net, from_fac: m.from_fac, to_fac: m.to_fac, drug: m.drug }))}">Approve</button>`}</span></li>`;
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
    + (escRows ? `<h3>Escalate instead of moving stock</h3><ul class="plain esc">${escRows}</ul>` : "")
    + (state.meta.real_roads ? "" : '<p class="note">Road times in this network are estimated from straight-line distance. The demo network uses Google Maps road times.</p>');
  $("#moves").querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", () => approveMove(b)));
}

async function approveMove(b) {   // the district officer's one click: the transfer becomes an issue order
  const move = JSON.parse(b.dataset.approve);   // the day and network it was shown for, whatever is on screen by now
  const mine = move.net === state.net, drop = (day) => {   // the cached plan and district screens do not know about it yet
    for (const k of state.side.keys()) if (k === `plan|${day}` || k === `district|${day}` || k.startsWith(`district|${day}|`)) state.side.delete(k);
  };
  b.disabled = true;
  try {
    const o = await post("/api/approve", move);
    b.outerHTML = `<small class="done">Order ${esc(o.indent_id)} approved</small>`;
    if (!mine) return notify(`Issue order ${o.indent_id} is in the ledger.`);
    drop(o.day);
    if (o.day === state.day) refreshCommand();
    const from = place(facOf(o.from)), to = place(facOf(o.to));
    notify(`Issue order ${o.indent_id} is in the ledger: ${plural(o.courses, "course")} from ${from.phc}, ${from.wh} to ${to.phc}, ${to.wh}.`);
  } catch (e) {
    if (e.status !== 409) { b.disabled = false; return notify("That transfer could not be approved. Try again."); }
    notify("That transfer is no longer in the plan it was shown from. The plan has been reloaded.");
    if (!mine) return;
    drop(move.t);
    if (move.t === state.day) {
      try { renderMoves(await cached(state.side, `plan|${state.day}`, api(`/api/plan?t=${state.day}`))); } catch { /* the next day change fetches it again */ }
      refreshCommand();
    }
  }
}

async function writeBrief() {   // Gemini turns the evidence above into a brief in the officer's language
  const { f, j } = state.sel, t = state.day, lang = $("#briefLang").value, btn = $("#briefBtn"), out = $("#briefOut");
  const brief = (code) => get(api(`/api/brief/${f}/${j}?t=${t}&language=${encodeURIComponent(code)}`));
  const stale = () => state.sel.f !== f || state.sel.j !== j || state.day !== t;
  btn.disabled = true;
  $("#listen").hidden = $("#briefAudio").hidden = true;
  $("#briefSay").textContent = "";
  out.textContent = "Gemini is writing the brief…";
  try {
    let b = await brief(lang), shown = lang, note = "";
    if (b.problems && lang !== "en") {   // a brief that fails its check is never shown: the English one is, with why
      note = `The ${state.meta.languages[lang].name} brief did not pass its check (${b.problems.join("; ")}), so here is the English one.`;
      b = await brief("en");
      shown = "en";
    }
    if (stale()) return;
    if (b.problems) {
      out.textContent = `The brief did not pass its check (${b.problems.join("; ")}), so it is not shown. The evidence above still stands.`;
      return;
    }
    out.innerHTML = `${note ? `<p class="note">${esc(note)}</p>` : ""}<p class="why" lang="${esc(shown)}">${esc(b.summary)}</p><p class="action" lang="${esc(shown)}"><strong>Next step:</strong> ${esc(b.next_step)}</p>`;
    $("#listen").dataset.lang = shown;
    $("#listen").hidden = !canSpeak(shown);
    loadGoogle();   // Gemini has now answered this server
  } catch (e) {
    if (stale()) return;
    out.textContent = e.status === 429 ? "Gemini calls are used up for today on this demo. The evidence above still stands."
      : "Gemini could not write the brief just now. The evidence above still stands.";
  } finally {
    btn.disabled = false;
  }
}

async function listenBrief() {   // Gemini-TTS reads the brief on screen aloud: paid once a brief, then served again
  const { f, j } = state.sel, t = state.day, btn = $("#listen"), audio = $("#briefAudio"), lang = btn.dataset.lang;
  const say = (message) => { if ($("#briefSay")) $("#briefSay").textContent = message; };
  btn.disabled = true;
  say("Gemini-TTS is reading the brief aloud. This takes about 20 seconds the first time; after that it plays at once.");
  try {
    const r = await fetch(api(`/api/brief/${f}/${j}/audio?t=${t}&language=${encodeURIComponent(lang)}`));
    if (state.sel.f !== f || state.sel.j !== j || state.day !== t) return;
    if (!r.ok) {
      const why = (await r.json().catch(() => ({}))).detail;
      if ([422, 503].includes(r.status)) btn.hidden = true;   // no voice for it here: stop offering one
      return say(r.status === 429 ? "Gemini calls are used up for today on this demo."
        : r.status === 503 ? "Gemini-TTS could not read the brief aloud on this server." : `It could not be read aloud${why ? `: ${why}` : ""}.`);
    }
    if (audio.src) URL.revokeObjectURL(audio.src);
    audio.src = URL.createObjectURL(await r.blob());
    audio.hidden = false;
    say("");
    audio.play().catch(() => {});   // a refused autoplay still leaves the controls
    loadGoogle();
  } catch {
    say("The server could not be reached. Try again.");
  } finally {
    btn.disabled = false;
  }
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
  const whs = Object.fromEntries(n.exports.map((ex) => [ex.state, new Set(ex.rows.map((r) => r.warehouse)).size]));
  const flags = Object.entries(n.view).flatMap(([drug, v]) => {
    const j = drugOf(drug), out = [], st = v.short_states[0];
    if (v.national) out.push(`<li><strong>${drugName(j)}</strong>: warehouses in ${v.short_states.map(stateName).join(" and ")} have stopped receiving it. Likely national supply failure. In testing this flag caught every injected national failure, about a month in and before most of the PHC stock-outs it caused.</li>`);
    else if (st) out.push(`<li><strong>${drugName(j)}</strong>: ${v.starved[st]} of ${whs[st]} warehouses in ${stateName(st)} have stopped receiving it, and no other state shows the same. Likely a state procurement failure, for ${stateName(st)} to fix.</li>`);
    else if (v.surge) out.push(`<li><strong>${drugName(j)}</strong>: demand is surging across states. Raise indents.</li>`);   // a supply break outranks a surge, as in triage
    return out;
  }).join("");
  const priors = Object.entries(n.priors).map(([drug, p]) =>
    `<tr><th scope="row">${drugName(drugOf(drug))}</th><td class="num">${pct(p.rho)}</td><td class="num">${pct(p.tau)}</td></tr>`).join("");
  $("#national").innerHTML = `<div class="scroll"><table class="data"><thead><tr><th scope="col">State</th><th scope="col">Share of PHC medicines</th><th scope="col" class="num">Stocked</th><th scope="col" class="num">Running short</th><th scope="col" class="num">Empty</th><th scope="col" class="num">Kept inside the state</th><th scope="col" class="num">Sent to the national view</th></tr></thead><tbody>${rows}</tbody></table></div>
    <h3>Patterns across states</h3>${flags ? `<ul class="plain">${flags}</ul>` : `<p class="note">No medicine shows a state-wide or cross-state pattern today.</p>`}
    <details><summary>What states get back</summary>
      <p class="note">National medians a new state can start from instead of waiting a month: how closely prescribing follows the rulebook, and how far registers can be trusted. In testing, a state three days in had fewer false alarms with these than with its own three days in 7 of 10 cases (more in 1), matching a month of its own history.</p>
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
  const few = (x) => x.toFixed(x < 0.1 ? 3 : 2);   // the table and the sentence above it print the same figure
  $("#proof").innerHTML = `<thead><tr><th scope="col">Method</th><th scope="col">Stock-outs of 4+ days caught</th><th scope="col">Warned before the shelf emptied</th><th scope="col">Typical warning</th><th scope="col" class="num">False alarms per medicine per year</th></tr></thead>
    <tbody>${rows.map(([n, r], k) => `<tr${k ? "" : ' class="lead"'}><th scope="row">${n}</th>${meter(r.recall_4d)}${meter(r.early)}<td>${lead(r.median_lead)}</td><td class="num">${few(r.false_per_series_year)}</td></tr>`).join("")}</tbody>`;
  const a = m.model, d = m.drug_only, same = pct(a.recall_4d) === pct(d.recall_4d), reg = m.register_best;
  $("#takeaway").innerHTML = `<strong>In short:</strong> ${same ? `Anumaan catches the same share of stock-outs as reading dispensing alone (${pct(a.recall_4d)} of those lasting 4+ days)`
    : `Anumaan catches ${pct(a.recall_4d)} of stock-outs lasting 4+ days, against ${pct(d.recall_4d)} for reading dispensing alone`}, with ${few(a.false_per_series_year)} false alarms per medicine per year instead of ${few(d.false_per_series_year)}${a.false_per_series_year ? ` (${fmt(d.false_per_series_year / a.false_per_series_year)} times fewer)` : ""}.${reg ? ` The stock register, tuned to the same false-alarm budget, catches ${pct(reg.recall_4d)}.` : ""}${d.early > a.early ? ` Dispensing alone warns before the shelf empties a little more often (${pct(d.early)} against ${pct(a.early)}).` : ""}`;
  const day = m.empty_days;
  $("#dayNote").textContent = `Day by day, on the days a shelf was truly empty, Anumaan called it empty on ${pct(day.model.found)} of them and was right ${pct(day.model.right)} of the times it said so. The register showed under half a day of use on ${pct(day.register.found)} of those days.`;
  $("#triageNote").textContent = `Where it broke is read from the warehouse ledger: right for ${pct(m.triage_7d.accuracy)} of alarms a week after they start, against ${pct(m.triage_majority)} for always guessing the commonest cause. Early warning comes from staff rationing and from deliveries minus dispensing; where staff never ration, Anumaan still warns before the shelf empties for more than half of outages.`;
}

function staffMark(x) {   // one cadre at one PHC: the attendance mark, questioned or not
  const truth = x.true_present === undefined ? "" : `<small>${x.true_present ? "Truly at work" : "Truly away"}</small>`;
  if (!x.in_position) return '<span class="quiet">Vacant</span>';
  if (x.verify) return `<span class="badge out">Check</span>${truth}`;
  return (x.marked_present ? '<span class="badge">Present</span>' : '<span class="quiet">Absent</span>') + truth;
}

function renderCare(b) {   // beds and staff for every PHC: what to act on first, then the whole network
  const s = b.summary, where = (f, st = true) => { const p = place(f); return `<strong>${p.phc}</strong>, ${p.wh}${st ? `, ${p.st}` : ""}`; };
  const full = b.rows.filter((r) => r.pressure).sort((x, y) => y.full_7d - x.full_7d);      // the longest-full first
  const checks = b.rows.flatMap((r) => r.staff.filter((x) => x.verify).map((x) => [r, x])).sort((x, y) => y[1].verify_7d - x[1].verify_7d);
  const rest = (n, what) => (n > 8 ? `<p class="note">And ${n - 8} more ${what}, in the table below.</p>` : "");
  $("#careCount").textContent = full.length + checks.length;
  const stat = (n, label, note) => `<div class="stat"><strong>${fmt(n)}</strong><span>${label}</span><small>${note}</small></div>`;
  const beds = full.slice(0, 8).map((r) => `<li><span class="tag nat">Every bed taken</span><span>${where(r.f)}: all ${r.capacity} beds in use, as on ${r.full_7d} of the last 7 days${r.early_share_7d ? `; ${pct(r.early_share_7d)} of last week's discharges were early` : ""}.
    ${r.refer ? `Nearest free bed: ${where(r.refer.f, false)}, ${road(r.refer.minutes)}, ${r.refer.free} free.` : `No PHC within ${b.refer_minutes} minutes has a free bed. Refer to the community health centre.`}${r.true_occupied === undefined ? "" : ` <em>Actual: ${r.true_occupied} occupied.</em>`}</span></li>`).join("");
  const marks = checks.slice(0, 8).map(([r, x]) => `<li><span class="tag">Check attendance</span><span>${where(r.f)}: ${esc(CADRE[x.cadre].toLowerCase())} marked present, but ${x.acts} of about ${Math.round(x.expected)} expected tasks are in the care record${x.verify_7d > 1 ? `. Flagged on ${x.verify_7d} of the last 7 days` : ""}.${x.true_present === undefined ? "" : ` <em>Actual: ${x.true_present ? "at work" : "away"}.</em>`}</span></li>`).join("");
  const audit = b.rows.filter((r) => r.audit && r.audit.marked / r.audit.away >= 0.2).sort((x, y) => y.audit.marked / y.audit.away - x.audit.marked / x.audit.away)
    .slice(0, 5).map((r) => `<li><span class="tag">Audit the register</span><span>${where(r.f)}: in the last ${b.audit_days} days the care record showed a role away ${r.audit.away} times, and the attendance feed marked it present on ${r.audit.marked} of them (${pct(r.audit.marked / r.audit.away)}).</span></li>`).join("");
  const groups = new Map();
  b.rows.forEach((r) => { const wh = state.meta.facilities[r.f].wh; groups.set(wh, [...(groups.get(wh) || []), r]); });
  const body = [...groups.values()].map((rows) => `<tbody>${rows.map((r, i) => {
    const p = place(r.f), dots = Array.from({ length: r.capacity }, (_, k) => `<i class="${k < r.occupied ? "on" : ""}"></i>`).join("");
    return `<tr>${i ? "" : `<th scope="rowgroup" rowspan="${rows.length}" class="wh">${p.wh}<small>${p.st}</small></th>`}<th scope="row">${p.phc}${r.as_of === undefined ? "" : `<small>Last report: day ${r.as_of}</small>`}</th>
      <td><span class="beds" aria-hidden="true">${dots}</span><small>${r.occupied} of ${r.capacity}${r.pressure ? ", full" : ""}</small></td>${r.staff.map((x) => `<td>${staffMark(x)}</td>`).join("")}</tr>`;
  }).join("")}</tbody>`).join("");
  $("#careBoard").innerHTML = `<div class="stats">${stat(s.beds - s.occupied, "Beds free", `of ${fmt(s.beds)} across the network`)}${stat(s.full, s.full === 1 ? "PHC with every bed taken" : "PHCs with every bed taken", s.no_bed_near ? `${s.no_bed_near} with no free bed nearby` : "each has a free bed nearby")}${stat(s.verify, s.verify === 1 ? "Attendance mark to check" : "Attendance marks to check", "marked present, no work recorded")}${stat(s.vacant, "Posts vacant", "of the sanctioned strength")}</div>
    <h3>Beds: where to send the next patient</h3>${beds ? `<ul class="plain esc">${beds}</ul>${rest(full.length, "full PHCs")}` : '<p class="note">No PHC is full today.</p>'}
    <h3>Attendance: marks the care record does not back</h3>${marks ? `<ul class="plain esc">${marks}</ul>${rest(checks.length, "marks to check")}` : '<p class="note">Every attendance mark today is backed by that role’s work in the care record, or the day was too quiet to tell.</p>'}
    <h3>Attendance registers to audit</h3>${audit ? `<ul class="plain esc">${audit}</ul>` : '<p class="note">No PHC’s attendance marks disagree with its care record often enough to single out yet.</p>'}
    <details><summary>Every PHC</summary><div class="scroll"><table class="data board"><thead><tr><th scope="col">District warehouse</th><th scope="col">PHC</th><th scope="col">Beds</th>${state.meta.cadres.map((k) => `<th scope="col">${CADRE[k]}</th>`).join("")}</tr></thead>${body}</table></div></details>`;
}

/* ---------- command center: one district's day in plain words, and its map ---------- */
const plural = (n, one, many = `${one}s`) => `${fmt(n)} ${fmt(n) === "1" ? one : many}`;   // as the number reads
const order = (wh) => state.meta.facilities.findIndex((x) => x.wh === wh);
function districtName(wh) {   // "Warehouse C, State 1"; a network that names its districts: "Ernakulam"
  const p = place(order(wh));
  return state.meta.names.warehouses?.[wh] ? p.wh.replace(/ warehouse$/, "") : `${p.wh}, ${p.st}`;
}
function districtFor(t, net) {   // the chosen district's screen, or the network's when "Whole network" is chosen
  // first look: the district of the signal the network opens on (the demo's hidden stock-out), else the one with the most to do
  if (state.wh === undefined) state.wh = state.meta.facilities[state.meta.start.f]?.wh ?? net.busiest;
  return state.wh ? cached(state.side, `district|${t}|${state.wh}`, api(`/api/district?t=${t}&wh=${encodeURIComponent(state.wh)}`)) : net;
}
async function refreshCommand() {   // after a new pick of district, or an approval: the same day, fetched again where it changed
  const t = state.day, truth = state.truth;
  try {
    const [net, plan, board] = await Promise.all([cached(state.side, `district|${t}`, api(`/api/district?t=${t}`)),
      cached(state.side, `plan|${t}`, api(`/api/plan?t=${t}`)), cached(state.side, `care|${t}|${truth}`, api(`/api/care?t=${t}${truth ? "&truth=true" : ""}`))]);
    const d = await districtFor(t, net);
    if (state.day === t) renderCommand(net, d, plan, board);
  } catch {
    notify("The district screen could not load. Try again.");
  }
}

function broke(where) {   // /api/district's "where it broke" for one medicine, as a sentence
  const part = (w) => (w.level === "DEMAND-SURGE" ? "demand is up, not a break" : `it broke ${w.words}`);
  if (where.length === 1) return `${cap(part(where[0]))}.`;
  return `${where.map((w, i) => `${i ? "at" : "At"} ${plural(w.phcs, "PHC")}, ${part(w)}`).join("; ")}.`;
}

function renderCommand(net, d, plan, board) {   // the district officer's first screen, from /api/district
  const home = (f) => d.wh && state.meta.facilities[f].wh === d.wh;
  const at = (f) => { const p = place(f); return home(f) ? p.phc : `${p.phc}, ${p.wh}`; };
  const districts = [...net.districts].sort((x, y) => order(x.wh) - order(y.wh));
  $("#district").innerHTML = `<option value="">Whole network (${districts.reduce((s, x) => s + x.todo, 0)} to do)</option>`
    + districts.map((x) => `<option value="${esc(x.wh)}"${x.wh === d.wh ? " selected" : ""}>${districtName(x.wh)} (${x.todo} to do)</option>`).join("");
  $("#district").disabled = false;
  $("#ccTitle").innerHTML = d.wh ? `Day ${d.day} in ${districtName(d.wh)}` : `Day ${d.day} across the network`;
  $("#ccSub").textContent = `${plural(d.phcs, "PHC")}${d.behind ? `; ${d.behind} still show an older report` : ", all reported"}.`;

  const ff = d.footfall, change = ff.change === null ? "" : Math.round(ff.change)
    ? ` (${ff.change > 0 ? "+" : "−"}${Math.abs(Math.round(ff.change))}%)` : " (about the same)";
  const ups = ff.up.map((x, i) => `${esc(x.condition.replace(/_/g, " "))} ${fmt(x.today)} (${i ? "" : "usual "}${fmt(x.usual)})`).join(", ");
  const foot = `${plural(ff.today, "diagnosis", "diagnoses")} recorded today${ff.usual === null ? "" : `, against a usual ${fmt(ff.usual)}${change}`}.${ups ? ` Up: ${ups}.` : ""}`;

  const meds = d.medicines, alarms = meds.reduce((s, m) => s + m.alarm, 0), hidden = meds.reduce((s, m) => s + m.hidden, 0);
  const med = (m) => `<button type="button" class="linkish" data-j="${m.j}">${drugName(m.j)}</button>`;
  const medText = meds.length ? `${plural(alarms, "shelf", "shelves")} in alarm; ${hidden ? `the register still shows ${hidden} of them as in stock` : "none hidden by the register"}.
    <ul class="cc-meds">${meds.slice(0, 3).map((m) => `<li>${med(m)}: ${plural(m.alarm, "PHC")} in alarm${m.hidden ? `, ${m.hidden} of them hidden by the register` : ""}. ${broke(m.where)}</li>`).join("")}
    ${meds.length > 3 ? `<li>Also in alarm: ${meds.slice(3).map(med).join(", ")}.</li>` : ""}</ul>` : "No medicine is in alarm today.";

  const b = d.beds, near = b.full.filter((x) => x.refer), far = b.full.length - near.length;
  const beds = !b.full.length ? "No PHC is full."
    : d.wh && b.full.length <= 3 ? b.full.map((x) => `${at(x.f)} is full: ${x.refer ? `send patients to ${at(x.refer.f)}, ${road(x.refer.minutes)} (${x.refer.free} free)`
      : `no PHC within ${board.refer_minutes} min has a free bed; refer to the community health centre`}.`).join(" ")
    : `${plural(b.full.length, "PHC")} full${near.length ? `, ${far ? `${near.length} of them` : "each"} with a free bed within ${road(Math.max(...near.map((x) => x.refer.minutes)))}` : ""}${far ? `; ${far} with no free bed within ${board.refer_minutes} min` : ""}.`;

  const s = d.staff, who = Object.entries(s.verify_by_cadre).filter(([, k]) => k).map(([c, k]) => plural(k, CADRE[c].toLowerCase())).join(", ");
  const staff = `${s.verify ? `${plural(s.verify, "attendance mark")} to check (${who}): marked present, but none of that work is in the care record.` : "No attendance marks to check."} ${plural(s.vacant, "post")} vacant.`;

  const a = d.actions, esc2 = new Map();
  for (const e of a.escalations) esc2.set(`${e.drug}|${e.level}`, (esc2.get(`${e.drug}|${e.level}`) || 0) + 1);
  const escText = [...esc2].map(([k, n]) => { const [drug, level] = k.split("|"); return `${drugName(drugOf(drug))} at ${plural(n, "PHC")} (${level === "NATIONAL" ? "likely national shortage" : "likely state procurement gap"})`; }).join("; ");
  const act = `${a.transfers ? `${a.to_approve === a.transfers ? plural(a.to_approve, "transfer") : `${fmt(a.to_approve)} of ${plural(a.transfers, "transfer")}`} to approve (${plural(a.courses, "course")}).` : "No transfers to approve."}${a.escalations.length ? ` ${plural(a.escalations.length, "escalation")}: ${escText}.` : ""}`;

  const row = (label, big, text, link = "") => `<div class="cc-row"><dt>${label}</dt><dd class="cc-big">${big}</dd><dd class="cc-text">${text}${link ? ` <a class="more" href="#${link[0]}">${link[1]}</a>` : ""}</dd></div>`;
  $("#ccBody").innerHTML = `<dl class="cc-rows">
    ${row("Medicines at risk", `<span${hidden ? ' class="alert"' : ""}>${meds.length}</span>`, medText, ["overview", "All stock signals"])}
    ${row("Diagnoses today", `${fmt(ff.today)}`, foot)}
    ${row("Beds free", `${fmt(b.free)}<small>of ${fmt(b.capacity)}</small>`, beds, ["care", "Beds and staff"])}
    ${row("Attendance marks to check", fmt(s.verify), staff, ["care", "Beds and staff"])}
    ${row("Do today", fmt(a.to_approve + a.escalations.length), act, ["transfers", "Review transfers"])}</dl>
    <p class="note cc-note">Diagnoses today (the district’s footfall) = ${esc(d.footfall_is)}. Usual = average of the previous ${d.usual_days} days.</p>`;
  $("#ccBody").setAttribute("aria-busy", "false");
  renderMap(d, plan);
}

const KIND = [["ok", "all stocked"], ["risk", "at risk"], ["out", "an empty shelf"], ["hidden", "a hidden stock-out"]];
function worst(f) {   // a PHC's worst shelf today: 3 hidden stock-out, 2 empty, 1 short or in alarm, 0 all stocked
  const J = state.meta.drugs.length;
  return Math.max(...state.meta.drugs.map((_, j) => { const c = state.byKey.get(f * J + j); return c.phantom ? 3 : c.regime === "OUT" ? 2 : c.regime === "SCARCE" || c.alarm ? 1 : 0; }));
}
function urgent(fs, js) {   // the most urgent of these PHCs x medicines, as the signal list ranks them; surer of empty first
  const J = state.meta.drugs.length, key = (c) => (c.phantom ? 0 : c.regime === "OUT" ? 1 : c.regime === "SCARCE" ? 2 : c.alarm ? 3 : 4) - c.p[2] / 2;
  return fs.flatMap((f) => js.map((j) => state.byKey.get(f * J + j))).reduce((x, y) => (key(y) < key(x) ? y : x));
}
function openDetail(c) {   // from the command center into the existing investigation of one PHC x medicine, its heading in view
  history.pushState(null, "", "#overview");
  navigate();
  select(c.f, c.j);
  $("#detail").focus({ preventScroll: true });
  $("#detail").scrollIntoView({ block: "start" });   // under the replay bar: scroll-margin-top
}

let mapArgs;
function renderMap(d, plan) {   // inline SVG, no tiles and no key: the district's PHCs and the donors of its transfers, fitted
  mapArgs = [d, plan];
  const box = $("#ccMap"), fac = state.meta.facilities, mine = fac.map((x) => !d.wh || x.wh === d.wh);
  const pairs = new Map();   // one arrow per donor -> recipient, whatever the medicines
  for (const m of plan.transfers) {
    const a = facOf(m.from_fac), b = facOf(m.to_fac), k = `${a}>${b}`;
    if (mine[b]) pairs.set(k, [...(pairs.get(k) || []), m]);
  }
  const lead = new Set([...fac.keys()].filter((f) => mine[f]).concat([...pairs.keys()].map((k) => +k.split(">")[0])));
  const W = Math.max(300, box.clientWidth || 520), M = 30, kx = Math.cos((fac[[...lead][0]].lat * Math.PI) / 180);
  const X = (f) => fac[f].lon * kx, Y = (f) => -fac[f].lat, xs = [...lead].map(X), ys = [...lead].map(Y);
  const x0 = Math.min(...xs), y0 = Math.min(...ys), sx = Math.max(Math.max(...xs) - x0, 0.05), sy = Math.max(Math.max(...ys) - y0, 0.05);
  const s = Math.min((W - 2 * M) / sx, (Math.min(440, W) - 2 * M) / sy), H = Math.round(sy * s + 2 * M), ox = (W - sx * s) / 2;
  const pt = (f) => [ox + (X(f) - x0) * s, M + (Y(f) - y0) * s].map((v) => Math.round(v * 10) / 10);
  const named = Boolean(d.wh), labelled = pairs.size <= 12;
  const shown = [...fac.keys()].filter((f) => { const [x, y] = pt(f); return x >= 0 && x <= W && y >= 0 && y <= H; });
  const boxes = shown.filter((f) => lead.has(f)).map((f) => { const [x, y] = pt(f); return { x: x - 8, y: y - 8, w: 16, h: 16 }; });
  const clash = (c) => (c.x < 0 || c.y < 0 || c.x + c.w > W || c.y + c.h > H ? 1e9 : 0) + boxes.reduce((n, o) =>
    n + Math.max(0, Math.min(c.x + c.w, o.x + o.w) - Math.max(c.x, o.x)) * Math.max(0, Math.min(c.y + c.h, o.y + o.h) - Math.max(c.y, o.y)), 0);
  let dropped = 0;
  const spot = (cands, keep) => {   // a label goes where it covers least of what is drawn; unless kept, it is left out when every spot covers something
    const b = cands.reduce((x, y) => (clash(y) < clash(x) ? y : x));
    if (!keep && clash(b) > 0) { dropped++; return null; }
    boxes.push(b);
    return b;
  };
  // the district's own PHCs are named first and always; a donor's name, and each route's minutes, only where they fit
  const tags = new Map(shown.filter((f) => named && lead.has(f)).sort((f, g) => mine[g] - mine[f]).map((f) => {
    const [x, y] = pt(f), p = place(f), text = mine[f] ? p.phc : `${p.phc}, ${p.wh}`, w = 6.4 * text.length;
    const b = spot([{ x: x + 10, y: y - 8, w, h: 16, tx: x + 10, ty: y + 4, a: "start" }, { x: x - 10 - w, y: y - 8, w, h: 16, tx: x - 10, ty: y + 4, a: "end" },
      { x: x - w / 2, y: y - 26, w, h: 16, tx: x, ty: y - 14, a: "middle" }, { x: x - w / 2, y: y + 10, w, h: 16, tx: x, ty: y + 22, a: "middle" }], mine[f]);
    return [f, b ? `<text x="${b.tx}" y="${b.ty}" text-anchor="${b.a}">${text}</text>` : ""];
  }));
  const arcs = [...pairs].map(([k, ms]) => {
    const [a, b] = k.split(">").map(Number), [x1, y1] = pt(a), [x2, y2] = pt(b);
    const cx = (x1 + x2) / 2 - (y2 - y1) * 0.18, cy = (y1 + y2) / 2 + (x2 - x1) * 0.18;   // bent, so a two-way pair does not overlap
    const min = `${ms[0].minutes} min`, w = 7 * min.length + 10, on = (t, a1, c, a2) => (1 - t) ** 2 * a1 + 2 * (1 - t) * t * c + t ** 2 * a2;
    const len = Math.hypot(x2 - x1, y2 - y1) || 1, nx = -(y2 - y1) / len, ny = (x2 - x1) / len;   // off to the side, for short arcs
    // no further out than 60 px: a label farther from its arrow reads as belonging to another
    const l = labelled && spot([[0.5, 0], [0.38, 0], [0.62, 0], [0.26, 0], [0.74, 0], [0.5, 22], [0.5, -22], [0.5, 40], [0.5, -40], [0.5, 60], [0.5, -60]]
      .map(([t, o]) => ({ x: on(t, x1, cx, x2) + o * nx - w / 2, y: on(t, y1, cy, y2) + o * ny - 9, w, h: 18 })), false);
    const p = place(a), q = place(b), title = `<title>${p.phc}, ${p.wh} to ${q.phc}, ${q.wh}, ${road(ms[0].minutes)}: ${ms.map((m) => `${drugName(drugOf(m.drug))}, ${plural(m.courses, "course")}`).join("; ")}</title>`;
    // the arrow, and its minutes apart: every label is drawn above every arrow
    return [`<g class="arc">${title}<path d="M${x1},${y1}Q${cx},${cy} ${x2},${y2}" marker-end="url(#mapArrow)"/></g>`,
      l ? `<g class="arc">${title}<rect x="${l.x}" y="${l.y}" width="${w}" height="18" rx="4"/><text x="${l.x + w / 2}" y="${l.y + 13}">${min}</text></g>` : ""];
  });
  const count = [0, 0, 0, 0];
  let donors = 0;
  const pins = shown.map((f) => {   // only the district's own PHCs show their shelves; a PHC elsewhere is plain, even a donor
    const [cx, cy] = pt(f), k = mine[f] ? worst(f) : -1, p = place(f), main = lead.has(f), what = k < 0 ? "a donor in another district" : KIND[k][1];
    if (k >= 0) count[k]++;
    else donors += main;
    return `<g class="pin s-${k < 0 ? "away" : KIND[k][0]}${main ? "" : " dim"}" data-f="${f}"${main ? ` tabindex="0" role="button" aria-label="${p.phc}, ${p.wh}: ${what}. Open its most urgent medicine"` : ' aria-hidden="true"'}>${main ? `<title>${p.phc}, ${p.wh}: ${what}</title>` : ""}<circle cx="${cx}" cy="${cy}" r="${main ? 7 : 4.5}"/>${tags.get(f) || ""}</g>`;
  }).join("");
  const where = d.wh ? districtName(d.wh) : "the network";
  box.innerHTML = `<svg class="map" viewBox="0 0 ${W} ${H}" role="group" aria-label="Map of ${where}: ${plural(count[3], "PHC")} with a hidden stock-out, ${count[2]} with an empty shelf, ${count[1]} at risk, ${count[0]} all stocked; ${plural(pairs.size, "transfer route")} into it${donors ? `; ${plural(donors, "donor PHC")} in other districts` : ""}">
    <defs><pattern id="mapHatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5"/><rect width="2" height="5" class="gap"/></pattern>
      <marker id="mapArrow" viewBox="0 0 10 10" refX="21" refY="5" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" orient="auto"><path d="M0,0L10,5L0,10z"/></marker></defs>
    ${arcs.map((a) => a[0]).join("")}${arcs.map((a) => a[1]).join("")}${pins}</svg>
    <p class="grid-hint map-key"><span><i class="sw ok"></i>All stocked</span><span><i class="sw scarce"></i>At risk</span><span><i class="sw out"></i>Empty shelf</span><span><i class="sw out phantom"></i>Hidden stock-out: the register still shows stock</span>${donors ? '<span><i class="sw away"></i>Donor in another district</span>' : ""}<span><i class="arc-key"></i>Transfer${labelled ? ", road minutes" : ""}</span></p>
    <p class="note map-note">${esc(state.meta.sites_note)} ${state.meta.real_roads ? "Road minutes from the Google Maps Routes API, fetched once and cached." : "Road minutes estimated from straight-line distance."}${labelled ? "" : " Pick a district to see each arrow’s road minutes."}${dropped ? " Labels that would cover others are left out: point at a PHC or an arrow for its name or minutes." : ""}</p>`;
}

const CALL = { WAREHOUSE: "a warehouse failure", "STATE-PROCUREMENT": "a state procurement failure", "DEMAND-SURGE": "a demand surge" };   // whatif.py's words
function whatifVerdict(el, data, w) {   // a what-if's call on its own event: its area, and its medicine for a cut
  const fac = state.meta.facilities, j = w.medicine ? drugOf(w.medicine) : -1;
  const where = w.district ? districtName(w.district) : stateName("S0");
  const hot = data.cells.filter((c) => c.level === w.level && (w.district ? fac[c.f].wh === w.district : fac[c.f].st === "S0") && (j < 0 || c.j === j));
  const head = `<strong>Verdict, day ${data.day}:</strong> `;
  el.innerHTML = head + (data.day < w.day ? `the event starts on day ${w.day}.`
    : !hot.length ? `Anumaan has not called ${CALL[w.level]} in ${where} yet; the event started on day ${w.day}.`
    : `Anumaan calls ${CALL[w.level]} in ${where}: ${plural(hot.length, "alarm")} at ${plural(new Set(hot.map((c) => c.f)).size, "PHC")}${j < 0 ? "" : ` on ${drugName(j)}`}. Next step: ${esc(state.meta.actions[w.level])}.`);
}
function verdict(data) {   // a scripted replay's call, in one line: has Anumaan named a state-wide break today?
  const el = $("#verdict");
  if (!el) return;
  if (state.meta.scenario.whatif) return whatifVerdict(el, data, state.meta.scenario.whatif);
  const lv = ["NATIONAL", "STATE-PROCUREMENT"].find((x) => data.cells.some((c) => c.level === x));
  if (!lv) {
    const n = data.cells.filter((c) => c.alarm).length;
    el.innerHTML = `<strong>Verdict, day ${data.day}:</strong> no state-wide break called yet${n ? `; none of today’s ${plural(n, "PHC alarm")} traces back to the state’s supply` : ""}.`;
    return;
  }
  const hot = data.cells.filter((c) => c.level === lv), meds = [...new Set(hot.map((c) => c.j))].map(drugName);
  el.innerHTML = `<strong>Verdict, day ${data.day}:</strong> it broke ${lv === "NATIONAL" ? "in more than one state’s supply" : "in the state’s supply"}. ${plural(hot.length, "alarm")} at ${plural(new Set(hot.map((c) => c.f)).size, "PHC")}, on ${meds.slice(0, -1).join(", ")}${meds.length > 1 ? " and " : ""}${meds[meds.length - 1]}, trace back to it. Next step: ${esc(state.meta.actions[lv])}.`;
}

/* ---------- the Google services behind the page, as this server can honestly say ---------- */
const when = (s) => (s.includes("T")
  ? `${new Date(s).toLocaleString("en-GB", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })} IST`
  : new Date(`${s}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })).replace("Sept", "Sep");
const rowsOf = (name) => (state.google?.services || []).filter((r) => STACK.find(([n]) => n === name)[1].includes(r.service));
function renderStack() {
  $("#stack").innerHTML = STACK.map(([name, want]) => {
    const rows = rowsOf(name);
    if (name === "Text-to-Speech" && !rows.length) return "";   // only where this server offers a voice
    const status = !want.length ? "in the app" : rows.map((r) => r.status).sort((x, y) => LIVENESS.indexOf(x) - LIVENESS.indexOf(y))[0] || "";
    return `<li><button type="button" class="chip" data-chip="${esc(name)}" data-status="${esc(status)}" aria-haspopup="dialog"><i class="dot" aria-hidden="true"></i>${esc(name)}<small>${esc(status)}</small></button></li>`;
  }).join("");
}
async function loadGoogle() {
  try { state.google = await get("/api/google"); } catch { /* the chips keep what they last said */ }
  renderStack();
  renderBench();
}
function serviceRow(r) {
  const said = { live: `Live: seen by this server at ${r.at && when(r.at)}.`, configured: "Configured on this server, not yet seen answering.",
    "not configured": "Not configured on this server.", off: "Off on this server.", cached: `Cached: fetched ${r.as_of && when(r.as_of)}.` }[r.status]
    || `Offline${r.as_of ? `: last run ${when(r.as_of)}` : ""}.`;
  const tone = r.status === "live" ? "" : r.status === "configured" ? " carbon" : " muted";
  return `<section class="svc-row"><h3>${esc(r.service)}<span class="badge${tone}">${esc(cap(r.status))}</span></h3><p>${said}</p>
    <dl><dt>Role</dt><dd>${esc(cap(r.used_for))}</dd><dt>Detail</dt><dd>${esc(r.detail)}</dd><dt>Source</dt><dd>${esc(r.source)}</dd></dl></section>`;
}
async function openService(name) {
  await loadGoogle();   // statuses change as the page is used: ask again
  const plan = state.side.get(`plan|${state.day}`), rows = rowsOf(name);
  const ortools = `<section class="svc-row"><h3>OR-Tools<span class="badge carbon">In the app</span></h3><p>Google’s open-source solver, run inside this app. It makes no call to Google Cloud.</p>
    <dl><dt>Role</dt><dd>The transfer plan: which calm PHC sends how many treatment courses to which PHC in need, as many as it can over the shortest drives.</dd>
    <dt>Detail</dt><dd>Solved on this server as a min-cost flow each time a day’s plan loads.${plan?.transfers.length ? ` It planned day ${state.day}’s ${plural(plan.transfers.length, "transfer")}.` : ""}</dd><dt>Source</dt><dd>anumaan/planner.py</dd></dl></section>`;
  $("#svcTitle").textContent = name;
  $("#svcBody").innerHTML = (name === "OR-Tools" ? ortools : rows.length ? rows.map(serviceRow).join("") : '<p class="note">This server did not say how this service stands. Try again in a moment.</p>')
    + (state.google ? `<p class="note">As this server saw it at ${when(state.google.now)}. It says “live” only with the moment it saw the service answer, and probes nothing.</p>` : "");
  if (!$("#svc").open) $("#svc").showModal();
  $("#svc").dataset.chip = name;   // on close, focus goes back to this chip: the row was redrawn while it opened
}

function renderContext() {   // what is different about this network, shown above every view
  const m = state.meta, box = $("#context");
  box.hidden = !(m.scenario || m.live);
  if (m.scenario) {
    const s = m.scenario, w = s.whatif;
    const links = s.sources.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.label)}</a>`).join(" and ");
    const day = (t) => `<button type="button" class="linkish" data-day="${t}">day ${t}</button>`;   // the story's days: a click goes there
    const story = !w ? "" : `<dl class="story">
      <div><dt>The event</dt><dd>${day(w.day)}</dd></div>
      <div><dt>Anumaan called it</dt><dd>${w.called === null ? `not by day ${m.clock}` : `${day(w.called)}, as ${CALL[w.level]}`}</dd></div>
      <div><dt>First shelf truly empty</dt><dd>${w.first === null ? "none" : `${day(w.first)}${w.called === null ? ""
        : ` <small>(${w.first === w.called ? "the day of the call" : `${plural(Math.abs(w.called - w.first), "day")} ${w.first < w.called ? "before" : "after"} the call`})</small>`}`}</dd></div>
      <div><dt>Stock-outs after the call</dt><dd>${w.stockouts ? `${w.after} of ${w.stockouts}` : "none"}</dd></div></dl>`;
    box.innerHTML = `<div><strong>${esc(s.title)}</strong> <span class="tag">${w ? WHATIF_LABEL : "Scripted what-if, synthetic records"}</span>${w ? POST : ""}<p id="verdict" class="verdict"></p>${story}<p>${esc(s.script)}</p>
      <details><summary>What it follows, and what is real</summary><p class="quiet">${esc(s.event)}${links ? ` Reports: ${links}.` : ""}</p><p class="quiet">${esc(s.note)}</p></details></div>`;
  } else if (m.live) {
    const s = m.live, via = { "Pub/Sub": " The reports travel through Google Cloud Pub/Sub.", direct: " Pub/Sub is not configured on this copy, so the reports are applied directly." }[s.via] || " Press Start feed to send the next day’s reports.";
    const log = s.log.map((e) => { const p = place(e.f); return `<li>Day ${e.day}, ${p.phc}, ${p.wh}, ${p.st}: ${[e.raised.length ? `new alarm on ${e.raised.map(drugName).join(", ")}` : "", e.cleared.length ? `alarm cleared on ${e.cleared.map(drugName).join(", ")}` : ""].filter(Boolean).join("; ")}</li>`; }).join("");
    const ahead = s.day > s.through ? ` Every PHC has reported up to day ${s.through}; a paper PHC has reported further.` : "";
    box.innerHTML = `<div><strong>Live feed: day ${s.day} of ${s.last}</strong><p>A simulated network, not a real one: ${m.facilities.length} made-up PHCs in ${new Set(m.facilities.map((f) => f.st)).size} made-up states, played forward by the simulator. No real PHC is connected. Each PHC’s day arrives as one message: its diagnoses, dispensing slips, register balances, admissions and attendance. ${s.reported} of ${s.phcs} PHCs ${s.reported === 1 ? "has" : "have"} reported day ${s.day}${s.seconds === null ? "" : `, and their estimates were updated ${s.seconds} seconds after the reports were sent`}.${ahead}${via}</p>${log ? `<ul class="feed-log" aria-label="Latest changes">${log}</ul>` : ""}</div>
      <div class="context-actions"><button type="button" class="button" id="jumpFeed"${s.through >= s.last ? " disabled" : ""}>Jump 10 days</button><button type="button" class="button" id="paperDay">Add a paper PHC’s day</button><span id="restartBox"><button type="button" class="button" id="restartFeed">Restart feed</button></span>
        <span class="tag post">Paper PHC: added after the 30 Sep submission</span></div>`;
  }
}

function setClock(n) {   // the last day there is data for: fixed in a replay, moving in the live feed
  state.meta.clock = n;
  $("#day").max = n;
  $("#dayMax").textContent = state.meta.live ? state.meta.live.last : n;   // the live feed counts to its last day, as its box does
}

async function feedStep(n = 1) {   // send the next n days' reports, wait for every PHC's to land, then show the last of them
  try {
    const r = await fetch(`/api/live/step?days=${n}`, { method: "POST" });
    if (r.status === 409) { stopReplay(); return notify("The feed has reached its last day. Restart it to run again."); }
    if (!r.ok) throw new Error(`step answered ${r.status}`);
    const sent = await r.json();
    let s = await get("/api/live");
    // a Pub/Sub push lands within a second or two; wait on `through`, not `complete`: a paper PHC may be ahead of the feed
    for (const end = Date.now() + 4000 + 1000 * sent.days; s.through < sent.day && Date.now() < end;) {
      await new Promise((done) => setTimeout(done, 250));
      s = await get("/api/live");
    }
    if (state.net !== "live") return;
    state.meta.live = s;
    setClock(s.day);
    renderContext();
    state.cache.clear();
    state.side.clear();
    await show(Math.min(sent.day, s.day));
    loadGoogle();   // the Pub/Sub chip may have just seen a push
  } catch (e) {
    stopReplay();
    notify("The feed stopped: the reports could not be sent. Try again.");
  }
}

async function jumpFeed(b) {
  stopReplay();
  b.disabled = true;
  b.textContent = "Sending 10 days…";
  await feedStep(10);
  if (b.isConnected) { b.disabled = false; b.textContent = "Jump 10 days"; }   // renderContext has usually replaced it
}

function askRestart() {   // one feed is shared by everyone viewing it: ask on the page first (the viewer blocks confirm())
  $("#restartBox").innerHTML = `<span class="ask" role="group" aria-labelledby="restartAsk"><span id="restartAsk">Restart the feed for everyone viewing it? It goes back to day ${state.meta.live.history - 1}.</span>
    <button type="button" class="button primary" id="restartYes">Restart</button><button type="button" class="button" id="restartNo">Keep it</button></span>`;
  $("#restartNo").focus();
}

async function restartFeed(b) {
  b.disabled = true;
  stopReplay();
  try {
    const r = await fetch("/api/live/reset", { method: "POST" });
    if (!r.ok) throw new Error(`reset answered ${r.status}`);
    await init();
    $("#restartFeed")?.focus();   // the ask was replaced: keep the keyboard where it was
    notify("The feed is back at its first day.");
  } catch (err) {
    notify("The feed could not be restarted. Try again.");
    if (b.isConnected) b.disabled = false;
  }
}

/* ---------- what-if worlds: pick an event, the simulator plays it out, Anumaan reads it ---------- */
const WHATIF_LABEL = "What-if on synthetic records: not a forecast and not a test set";   // whatif.LABEL
const FIELDS = { warehouse: ["district", "medicine", "day"], state: ["medicine", "day"], emergency: ["district", "lift", "day"] };   // whatif.FIELDS: the API takes exactly these
function switchNet(net) {
  state.net = net;
  history.replaceState(null, "", `${net === "demo" ? location.pathname : `?net=${encodeURIComponent(net)}`}${location.hash}`);
  return init();
}
async function openWhatif() {
  if (!$("#whatif").open) $("#whatif").showModal();
  if (state.choices) return;
  try { state.choices = await get("/api/whatif"); } catch { $("#whatifSay").textContent = "The what-if choices could not load. Close this and try again."; return; }
  const c = state.choices, f = $("#whatifForm");
  $("#whatifMenu").innerHTML = c.menu.map((m) => `<li><button type="button" class="button wi-pick" data-key="${esc(m.key)}">${esc(m.label)}</button></li>`).join("");
  f.medicine.innerHTML = c.medicines.map((x) => `<option value="${esc(x)}">${medName(x)}</option>`).join("");
  f.lift.innerHTML = c.lifts.map((x, i) => `<option value="${x}"${i === Math.floor(c.lifts.length / 2) ? " selected" : ""}>× ${x}</option>`).join("");
  Object.assign(f.day, { min: c.days[0], max: c.days[1] });
  $("#wiDayHint").textContent = `Day ${c.days[0]} to ${c.days[1]}: Anumaan learns each PHC over its first 30 days, and the world runs on well past the event.`;
  f.querySelectorAll("[data-districts]").forEach((el) => { el.textContent = plural(Object.keys(c.districts[el.dataset.districts]).length, "district"); });
  $("#whatifPicks").hidden = f.hidden = false;
  wiFields();
}
function wiFields() {   // the fields the chosen event takes, and the chosen network's districts
  const f = $("#whatifForm"), c = state.choices, want = FIELDS[f.event.value], keep = f.district.value;
  f.district.innerHTML = Object.entries(c.districts[f.world.value]).map(([k, v]) => `<option value="${esc(k)}"${k === keep ? " selected" : ""}>${esc(v)}</option>`).join("");
  ["district", "medicine", "lift"].forEach((k) => { f[k].closest(".field").hidden = !want.includes(k); });
}
async function openWorld(net) {   // a world is built on its first request, one at a time; then every view reads it with ?net=
  const say = $("#whatifSay"), t0 = Date.now(), controls = $("#whatif").querySelectorAll("button, select, input");
  const tick = setInterval(() => { say.textContent = `Building this world on synthetic records… ${Math.round((Date.now() - t0) / 1000)} s`; }, 1000);
  say.textContent = "Building this world on synthetic records… it takes a few seconds.";
  controls.forEach((el) => { el.disabled = true; });
  try {
    await get(`/api/meta?net=${encodeURIComponent(net)}`);
    say.textContent = "";
    $("#whatif").close();
    await switchNet(net);
  } catch {
    say.textContent = "The world could not be built. Try again.";
  } finally {
    clearInterval(tick);
    controls.forEach((el) => { el.disabled = false; });
  }
}
async function buildWhatif(e) {
  e.preventDefault();
  const f = e.target, params = { world: f.world.value, event: f.event.value };
  for (const k of FIELDS[params.event]) params[k] = ["day", "lift"].includes(k) ? +f[k].value : f[k].value;
  try {
    const out = await post("/api/whatif", params);
    await openWorld(out.net);
  } catch (err) {
    $("#whatifSay").textContent = err.status === 422 ? `${err.message}.` : "The world could not be built. Try again.";
  }
}

/* ---------- a paper PHC: photos of its registers, read by Gemini, checked by a person, then into the live feed ---------- */
const START = Date.UTC(2026, 0, 1);   // feeds.START: calendar day 0 of the synthetic live feed
const dateOf = (t) => new Date(START + t * 864e5).toISOString().slice(0, 10);
const dayOf = (iso) => Math.round((Date.parse(`${iso}T00:00:00Z`) - START) / 864e5);
// the grammar's conditions, and every medicine the intake schema knows (anumaan/intake.py); "other" is left out of the counts
const COND = { fever: "Fever", pneumonia: "Pneumonia", acute_diarrhoea: "Acute diarrhoea", hypertension: "Hypertension", type2_diabetes: "Type 2 diabetes",
  uti: "Urinary tract infection", anaemia_pregnancy: "Anaemia in pregnancy", other: "Other: not tracked" };
const MEDS = ["amlodipine_5", "amoxicillin_500", "doxycycline_100", "erythromycin_250", "fosfomycin_3g", "ifa_tab", "levofloxacin_250", "metformin_500",
  "nitrofurantoin_100", "norfloxacin_400", "ors_sachet", "paracetamol_500", "trimethoprim_sulphamethoxazole_ds", "other"];
const PAGES = 4, PAGE_MB = 5;   // intake.MAX_IMAGES and MAX_BYTES; the server checks them again
const ik = {};   // the day being read: its PHC, date, pages, the read, the message it makes and the lines to check
// a server message as the page words it: a PHC by its name, a date as "2 Mar 2026"
const named = (s) => s.replace(/S\d+-W\d+-P\d+/g, (id) => { const f = facOf(id); if (f < 0) return id; const p = place(f); return `${p.phc}, ${p.wh}, ${p.st}`; })
  .replace(/\b\d{4}-\d{2}-\d{2}\b/g, when);
const base64 = (blob) => new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(r.result.split(",")[1]); r.onerror = fail; r.readAsDataURL(blob); });
const ikSay = (message) => { if ($("#ikSay")) $("#ikSay").textContent = message; };

function openIntake() {
  if (!$("#intake").open) $("#intake").showModal();
  if (ik.stage !== "check") intakePick();
}
async function intakePick() {
  (ik.pages || []).forEach((p) => { if (p.url.startsWith("blob:")) URL.revokeObjectURL(p.url); });
  Object.assign(ik, { stage: "pick", pages: [], sample: false });
  const s = state.meta.live;
  $("#intakeBody").innerHTML = `<form id="ikPick" class="ik-pick">
    <div class="ik-fields"><label class="field"><span>PHC</span><select name="phc">${state.meta.facilities.map((x, f) => { const p = place(f); return `<option value="${esc(x.id)}">${p.phc}, ${p.wh}, ${p.st}</option>`; }).join("")}</select></label>
      <label class="field"><span>Day of the registers</span><input name="date" type="date" required min="${dateOf(0)}" max="${dateOf(s.last)}" value="${dateOf(s.through + 1)}" aria-describedby="ikDateHint"></label></div>
    <p id="ikDateHint" class="note">Every PHC has reported up to ${when(dateOf(s.through))} (day ${s.through}), so a PHC’s next day is ${when(dateOf(s.through + 1))}.</p>
    <label class="field"><span>Photos of that day’s OPD and dispensing register pages: 1 to ${PAGES}, JPEG, PNG or WebP, up to ${PAGE_MB} MB each</span><input name="files" type="file" accept="image/jpeg,image/png,image/webp" multiple></label>
    <p id="ikSampleRow" class="ik-or" hidden>Or <button type="button" class="button" id="ikSample">Use the sample pages</button></p>
    <ul id="ikPages" class="ik-pages" aria-label="Pages to read"></ul>
    <div class="ik-actions"><button type="submit" class="button primary" id="ikRead" disabled>Read with Gemini</button><button type="button" class="button" id="ikRecorded" hidden>Use the recorded read (no Gemini call)</button></div>
    <p id="ikSay" class="ik-say" role="status"></p></form>`;
  if (state.sample === undefined) state.sample = await get("/samples/sample.json").catch(() => null);   // shipped only with tools/samples
  if (state.sample && $("#ikSampleRow")) $("#ikSampleRow").hidden = false;
}
function renderPages() {
  $("#ikPages").innerHTML = ik.pages.map((p, i) => `<li><img src="${esc(p.url)}" alt="Page ${i + 1}, ${esc(p.name)}"><span>${esc(p.name)}</span></li>`).join("");
  $("#ikRead").disabled = !ik.pages.length;
  $("#ikRecorded").hidden = !(ik.sample && state.sample.recorded_read);
}
async function useSample() {
  const s = state.sample, form = $("#ikPick"), f = facOf(s.message.phc), p = place(f);
  ikSay("Loading the sample pages…");
  try {
    ik.pages = await Promise.all(s.pages.map(async (name) => {
      const blob = await (await fetch(`/samples/${encodeURIComponent(name)}`)).blob();
      return { name, mime: blob.type, data: await base64(blob), url: `/samples/${encodeURIComponent(name)}` };
    }));
  } catch { return ikSay("The sample pages could not load. Try again."); }
  form.phc.value = s.message.phc;
  form.date.value = s.message.date;
  ik.sample = true;
  renderPages();
  ikSay(`Sample pages for ${p.phc}, ${p.wh}, ${p.st} on ${when(s.message.date)}: synthetic registers drawn in a handwriting font, not real ones.`);
}
async function useFiles(input) {
  const files = [...input.files];
  if (!files.length) return;
  const bad = files.length > PAGES ? `Choose 1 to ${PAGES} photos.`
    : files.some((x) => !["image/jpeg", "image/png", "image/webp"].includes(x.type) || x.size > PAGE_MB << 20) ? `Each photo must be a JPEG, PNG or WebP of at most ${PAGE_MB} MB.` : "";
  if (bad) { input.value = ""; return ikSay(bad); }
  ik.pages.forEach((p) => { if (p.url.startsWith("blob:")) URL.revokeObjectURL(p.url); });
  ik.pages = await Promise.all(files.map(async (x) => ({ name: x.name, mime: x.type, data: await base64(x), url: URL.createObjectURL(x) })));
  ik.sample = false;
  renderPages();
  ikSay("");
}
async function intakeRead(recorded) {   // one Gemini call reads every page; the recorded read of the sample makes none
  const form = $("#ikPick"), controls = form.querySelectorAll("button, select, input"), rec = state.sample?.recorded_read;
  const phc = recorded ? state.sample.message.phc : form.phc.value, date = recorded ? state.sample.message.date : form.date.value;
  const wait = "On the test pages a read took 42 seconds at the median, 70 at most.";   // anumaan/intake_eval.json: 42.2 s and 70.3 s
  const t0 = Date.now(), tick = recorded ? 0 : setInterval(() => ikSay(`Gemini is reading ${plural(ik.pages.length, "page")}… ${Math.round((Date.now() - t0) / 1000)} s. ${wait}`), 1000);
  ikSay(recorded ? "Checking the recorded read…" : `Gemini is reading ${plural(ik.pages.length, "page")}. ${wait}`);
  controls.forEach((el) => { el.disabled = true; });
  try {
    const out = recorded ? { ...(await post("/api/intake/check", { phc, date, read: rec.read })), read: rec.read, seconds: rec.seconds }
      : await post("/api/intake/read", { phc, date, images: ik.pages.map(({ mime, data }) => ({ mime, data })) });
    Object.assign(ik, { stage: "check", phc, date, read: out.read, message: out.message, review: out.review, seconds: out.seconds, recorded: recorded ? rec : null });
    renderIntakeCheck(true);
    if (!recorded) loadGoogle();   // Gemini has now answered this server
  } catch (e) {
    ikSay(e.status === 409 ? `${named(e.message)}.${ik.sample ? SAMPLE_ONLY : ""}`
      : e.status === 429 ? `Gemini calls are used up for today on this demo.${ik.sample && rec ? " Use the recorded read instead." : ""}`
      : e.status === 503 ? "Gemini could not read the pages just now. Try again."
      : [404, 422].includes(e.status) ? `${named(e.message)}.` : "The pages could not be sent. Check your connection and try again.");
  } finally {
    clearInterval(tick);
    if (form.isConnected) { controls.forEach((el) => { el.disabled = false; }); $("#ikRead").disabled = !ik.pages.length; }
  }
}
function renderIntakeCheck(focus) {   // the read as editable tables, the lines to check highlighted; nothing applied yet
  const { read, message: m, review } = ik, p = place(facOf(ik.phc)), body = $("#intakeBody");
  const open = Object.fromEntries([...body.querySelectorAll("details[data-t]")].map((d) => [d.dataset.t, d.open]));
  const flags = new Map(review.map((x) => [`${x.register}|${x.line}`, x.why]));
  const counts = review.reduce((o, x) => Object.assign(o, { [x.why]: (o[x.why] || 0) + 1 }), {});
  const dx = Object.values(m.diagnoses).reduce((a, b) => a + b, 0);
  const attrs = (reg, i, k, what) => `id="ik-${reg}-${i}-${k}" data-reg="${reg}" data-i="${i}" data-k="${k}" aria-label="OPD ${esc(read[reg][i].opd_no)}, ${what}"`;
  const text = (reg, i, k, what) => `<input ${attrs(reg, i, k, what)} type="text" value="${esc(read[reg][i][k])}" size="5">`;
  const num = (reg, i, k, what) => `<input ${attrs(reg, i, k, what)} type="number" min="0" step="${k === "days" ? 1 : "any"}" value="${esc(read[reg][i][k])}">`;
  const pick = (reg, i, k, what, keys, label) => `<select ${attrs(reg, i, k, what)}>${keys.map((x) => `<option value="${x}"${x === read[reg][i][k] ? " selected" : ""}>${label(x)}</option>`).join("")}</select>`;
  const na = (i) => `<input ${attrs("dispensing", i, "not_available", "marked not available")} type="checkbox"${read.dispensing[i].not_available ? " checked" : ""}>`;
  const row = (reg, i, cells) => `<tr${flags.has(`${reg}|${i}`) ? ' class="flagged"' : ""}>${cells}<td class="ik-why">${esc(flags.get(`${reg}|${i}`) || "")}</td></tr>`;
  const said = (reg, i) => `<td>${text(reg, i, "opd_no", "OPD number")}</td><td class="ik-written">${esc(read[reg][i].written_as)}</td>`;
  const table = (t, title, head, rows, empty) => {
    const bad = rows.filter((r) => r.includes('class="flagged"')).length;
    return `<details data-t="${t}"${open[t] ?? (bad > 0 || t === "opd") ? " open" : ""}><summary>${title}: ${plural(rows.length, "line")}${bad ? `, ${bad} to check` : ""}</summary>
      ${rows.length ? `<div class="scroll"><table class="data ik-table"><thead><tr><th scope="col">OPD no.</th><th scope="col">Written in the register</th>${head}<th scope="col">To check</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>` : `<p class="note">${empty}</p>`}</details>`;
  };
  const disp = read.dispensing.map((x, i) => [x, i]);
  body.innerHTML = `<p class="ik-sum" tabindex="-1">${ik.recorded ? `A read recorded earlier from ${esc(ik.recorded.model)} (${esc(when(ik.recorded.run_at))}, ${ik.seconds} s); no Gemini call was made now.` : `Read by Gemini in ${ik.seconds} s.`}
      For <strong>${p.phc}, ${p.wh}, ${p.st}</strong> on ${when(ik.date)} (day ${dayOf(ik.date)}), it adds ${plural(dx, "diagnosis", "diagnoses")} Anumaan tracks, ${plural(m.slips.length, "medicine given", "medicines given")} and ${plural(m.not_available.length, "“not available” slip")}.</p>
    <p class="ik-review${review.length ? " has" : ""}">${review.length ? `<strong>${plural(review.length, "line")} to check</strong>, highlighted below: ${Object.entries(counts).map(([w, n]) => `${esc(cap(w))} (${fmt(n)})`).join("; ")}.` : "No line needs checking."} Correct any line and the counts above update. <button type="button" class="linkish" id="ikSkip">Skip to Confirm and apply</button></p>
    ${table("opd", "Diagnoses (OPD register)", '<th scope="col">Condition</th>', read.opd.map((x, i) => row("opd", i, `${said("opd", i)}<td>${pick("opd", i, "condition", "condition", Object.keys(COND), (k) => COND[k])}</td>`)), "")}
    ${table("given", "Medicines given (dispensing register)", '<th scope="col">Medicine</th><th scope="col">Days</th><th scope="col">Units</th><th scope="col">Not available</th>',
      disp.filter(([x]) => !x.not_available).map(([, i]) => row("dispensing", i, `${said("dispensing", i)}<td>${pick("dispensing", i, "drug", "medicine", MEDS, (k) => (k === "other" ? "Other: not tracked" : medName(k)))}</td><td>${num("dispensing", i, "days", "days")}</td><td>${num("dispensing", i, "units", "units given")}</td><td>${na(i)}</td>`)),
      "No medicine given on these pages.")}
    ${table("na", "Marked not available", '<th scope="col">Medicine</th><th scope="col">Not available</th>',
      disp.filter(([x]) => x.not_available).map(([, i]) => row("dispensing", i, `${said("dispensing", i)}<td>${pick("dispensing", i, "drug", "medicine", MEDS, (k) => (k === "other" ? "Other: not tracked" : medName(k)))}</td><td>${na(i)}</td>`)),
      "No line on these pages says a medicine was not available.")}
    <p class="apply-note"><strong>Nothing is applied until you confirm.</strong> Confirming adds this day to ${p.phc}’s record in the live network, once.</p>
    <div class="ik-actions"><button type="button" class="button primary" id="ikConfirm">Confirm and apply</button><button type="button" class="button" id="ikBack">Start again</button></div>
    <p id="ikSay" class="ik-say" role="status"></p>`;
  if (focus) body.querySelector(".ik-sum").focus();   // keyboard and screen-reader users land on what was read, not on the page
}
const SAMPLE_ONLY = " The sample fits a freshly restarted feed only: restart the feed to use it again.";
async function ikEdit(el) {   // a person corrects a line: the server re-derives the day and the lines to check (no Gemini call)
  const x = ik.read[el.dataset.reg][+el.dataset.i], id = el.id;
  x[el.dataset.k] = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
  try {
    Object.assign(ik, await post("/api/intake/check", { phc: ik.phc, date: ik.date, read: ik.read }));
    renderIntakeCheck();
    document.getElementById(id)?.focus();
  } catch (e) {
    ikSay(e.status === 422 ? `That line cannot be used: ${named(e.message)}.` : e.status === 409 ? `${named(e.message)}. Nothing can be applied.${ik.sample ? SAMPLE_ONLY : ""}`
      : "The check could not run. Try again.");
  }
}
async function intakeConfirm(b) {   // into the live network, once; then the PHC's estimates, before and after
  const t = dayOf(ik.date), f = facOf(ik.phc), mine = (d) => d.cells.filter((c) => c.f === f);
  b.disabled = true;
  ikSay("Applying the day…");
  try {
    const before = t > 0 ? mine(await get(`/api/day/${t - 1}?net=live`)) : [];
    const res = await post("/api/intake/confirm", ik.message);
    const after = mine(await get(`/api/day/${res.day}?net=live`));
    ik.stage = "done";
    renderIntakeDone(res, f, before, after);
    refreshLive(res.status);
  } catch (e) {
    b.disabled = false;
    ikSay(e.status === 409 ? `${named(e.message)}. Nothing was applied.${ik.sample ? SAMPLE_ONLY : ""}` : e.status === 422 ? `${named(e.message)}. Nothing was applied.` : "The day could not be applied. Nothing was applied; try again.");
  }
}
function renderIntakeDone(res, f, before, after) {
  const p = place(f), t = res.day;
  const shelf = (c) => (!c ? "No report" : `<span class="badge ${REGIME[c.regime][0]}${c.phantom ? " phantom" : ""}">${c.phantom ? "Hidden stock-out" : REGIME[c.regime][1]}</span>${c.alarm ? "<small>In alarm</small>" : ""}${c.shadow === null ? "" : `<small>${fmt(c.shadow)} days of use left</small>`}`);
  let changed = 0, recount = 0;
  const rows = after.map((c) => {
    const b = before.find((x) => x.j === c.j), moved = Boolean(b) && (b.regime !== c.regime || b.alarm !== c.alarm);
    changed += moved;
    recount += Boolean(b) && b.shadow !== null && c.shadow !== null && fmt(b.shadow) !== fmt(c.shadow);   // as the table shows them
    return `<tr${moved ? ' class="flagged"' : ""}><th scope="row">${drugName(c.j)}</th><td data-label="Day ${t - 1}, before">${shelf(b)}</td><td data-label="Day ${t}, with the paper day">${shelf(c)}</td></tr>`;
  }).join("");
  $("#intakeBody").innerHTML = `<p class="ik-sum" tabindex="-1"><span class="done">Applied.</span> ${p.phc}, ${p.wh}, ${p.st} has reported ${when(ik.date)} (day ${t}) from its paper registers, and its estimates have been worked out again.</p>
    <div class="scroll"><table class="data ik-table ik-after"><thead><tr><th scope="col">Medicine</th><th scope="col">Day ${t - 1}, before</th><th scope="col">Day ${t}, with the paper day</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="note">${recount ? `Worked out again from the paper day: days of use left changed for ${recount} of ${plural(after.length, "medicine")}. ` : ""}${changed ? `${plural(changed, "medicine")} changed shelf reading or alarm, highlighted.` : "No shelf reading or alarm changed."}</p>
    <div class="ik-actions"><button type="button" class="button primary" id="ikOpen" data-f="${f}" data-t="${t}">Open ${p.phc} on day ${t}</button><button type="button" class="button" id="ikAgain">Add another day</button></div>`;
  $("#intakeBody .ik-sum").focus();
}
async function refreshLive(status) {   // the live network changed under the page: redraw it on the day the officer is on
  if (state.net !== "live") return;
  state.meta.live = status;
  setClock(status.day);
  renderContext();
  state.cache.clear();
  state.side.clear();
  await show(Math.min(state.day, status.day));
}

function realChart(s) {   // areas flagged each month for either fingerprint, with the first official notice marked
  const W = 240, H = 64, pad = 14, n = s.months.length, bw = W / n, at = s.months.indexOf(s.notice_month);
  const bar = (v, i, off, col, what) => (v ? `<rect x="${i * bw + off}" y="${H - pad - (v / s.icbs) * (H - pad - 4)}" width="${bw / 2 - 1}" height="${(v / s.icbs) * (H - pad - 4)}" fill="${col}"><title>${month(s.months[i])}: ${v} of ${s.icbs} ${what}</title></rect>` : "");
  const bars = s.months.map((_, i) => bar(s.substituting.flagged[i], i, 1, "var(--sub)", "substituting") + bar(s.cut_short.flagged[i], i, bw / 2, "var(--scarce)", "cutting courses short")).join("");
  return svg(W, H, `Areas flagged in each of ${n} months; the first official notice came in ${month(s.notice_month)}`, `
    <line x1="0" x2="${W}" y1="${H - pad}" y2="${H - pad}" stroke="var(--rule)"/>${bars}
    <line x1="${at * bw}" x2="${at * bw}" y1="0" y2="${H - pad}" stroke="var(--ink)" stroke-dasharray="3 3"/>
    <text x="0" y="${H - 3}" font-size="10" fill="var(--muted)">${month(s.months[0])}</text>
    <text x="${W}" y="${H - 3}" font-size="10" fill="var(--muted)" text-anchor="end">${month(s.months[n - 1])}</text>`);
}
const month = (m) => new Date(`${m}-01T00:00:00Z`).toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" }).replace("Sept", "Sep");   // as when() writes it

function renderReal(d) {   // the premise, asked of real dispensing records: England's, because India publishes none per PHC
  const most = (x, s) => { const v = Math.max(...x.flagged); return v ? `<strong>${v} of ${s.icbs}</strong><small>${month(s.months[x.flagged.indexOf(v)])}</small>` : "None"; };
  const rows = d.shortages.map((s) => {
    const first = [...s.cut_short.wide_months, ...s.substituting.wide_months].sort()[0], lead = s.months.indexOf(s.notice_month) - s.months.indexOf(first);
    return `<tr><th scope="row">${esc(s.name)}</th><td>${month(s.notice_month)}</td><td>${most(s.substituting, s)}</td><td>${most(s.cut_short, s)}</td>
      <td>${first ? `${month(first)}<small>${lead > 0 ? `${lead} ${lead === 1 ? "month" : "months"} before the notice` : lead ? "after the notice" : "the month of the notice"}</small>` : "Never"}</td><td>${realChart(s)}</td></tr>`;
  }).join("");
  const quiet = Math.max(...d.placebos.flatMap((p) => p.substituting.flagged)), cut = d.placebos.reduce((n, p) => n + p.cut_short.wide_months.length, 0);
  const swide = d.placebos.some((p) => p.substituting.wide_months.length);
  $("#realBody").innerHTML = `<p class="note">India publishes no PHC-level dispensing records; the check below uses India’s district-month HMIS totals. England publishes what every GP practice dispenses each month, so this asks one thing of real data: when a medicine is officially declared short, do the fingerprints Anumaan reads show up in routine dispensing, in many areas at once? An area is one of England’s ${d.shortages[0].icbs} integrated care boards. It is flagged when it moves more than ${d.rule.z} robust standard deviations from its own ${d.rule.baseline_months} months before the first official notice.</p>
    <div class="scroll"><table class="data real"><thead><tr><th scope="col">Declared shortage</th><th scope="col">First official notice</th><th scope="col">Substitutes’ share up: most areas flagged</th><th scope="col">Courses cut short: most areas flagged</th><th scope="col">Many areas at once, first met</th><th scope="col">Areas flagged, by month</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="chart-key" aria-hidden="true"><span><i class="sw sub"></i>Substituting</span><span><i class="sw scarce"></i>Cutting courses short</span><span><i class="dash"></i>First official notice</span></p>
    <p class="note">Substitution showed in both shortages and cut-short courses in one, each in many areas at once, and both times the first signs came before the first official notice. That is not proof of early warning. Those earlier months fall inside each area’s own baseline year and are scored in hindsight, the makers reported supply trouble covering those months, and the data came out about two months in arrears. The Creon figures also lean on how capsules are counted; the README says how far.</p>
    <p class="note">Two medicines with no declared shortage, over the same months: flagged for substitution in at most ${quiet} of ${d.shortages[0].icbs} areas${swide ? "" : ", never many at once"}. On cut-short courses the many-areas rule fired in ${cut} of their ${d.placebos.reduce((n, p) => n + p.months.length, 0)} months, as one medicine’s tablets per prescription drifted down.</p>
    <p class="note">This tests the premise only. Anumaan’s filter was not run on this data; the figures are monthly totals per area, in another health system, for two shortages picked by hand. Data: <a href="${esc(d.source.url)}" target="_blank" rel="noopener">${esc(d.source.dataset)}</a>, ${esc(d.source.publisher)}, fetched ${esc(d.source.fetched)}. ${esc(d.source.attribution)}</p>`;
  $("#real").hidden = false;
}
async function loadReal() {
  try { renderReal(await get("/api/real")); } catch (e) { /* a copy without the cached data simply has no panel */ }
}

function renderIndia(d) {   // the premise, asked of India's own HMIS records: district-month, rules fixed before any result
  const v = d.headline_version, other = Object.keys(d.decision).find((k) => k !== v);
  const fy = (k) => { const ys = d.years[k]; return `FY ${ys[0].slice(0, 4)}-${ys[ys.length - 1].slice(-2)}`; };
  const word = (x) => cap(x.reading || x.label), tone = { refuted: " out", inconclusive: " scarce" };
  const share = (k, n) => `${fmt(k)} of ${fmt(n)}<small>${n ? ((100 * k) / n).toFixed(1) : "0.0"}%</small>`;
  const role = { decision: "The pre-registered test", placebo: "Placebo", secondary: "Secondary" };
  const rows = d.rows.map((r) => {
    const x = r[v], y = other && r[other];
    return `<tr><th scope="row">${esc(r.name)}<small>${role[r.role] || esc(r.role)}</small></th><td>${share(x.empty_flagged, x.empty_months)}</td><td>${share(x.stocked_flagged, x.stocked_months)}</td>
      <td class="num">${x.rr.toFixed(2)}<small>${x.ci[0].toFixed(2)} to ${x.ci[1].toFixed(2)}</small></td><td>${esc(word(x))}${y ? `<small>${fy(other)} alone: ${esc(word(y).toLowerCase())}</small>` : ""}</td></tr>`;
  }).join("");
  // in the open: what it tests, that it is not about hidden stock-outs, which districts count, and the calcium placebo's ratio
  const s = d.source, OPEN = [0, 1, 2, 5], shown = OPEN.map((i) => d.caveats[i]).filter(Boolean), rest = d.caveats.filter((_, i) => !OPEN.includes(i));
  $("#indiaBody").innerHTML = `<p class="decision"><span class="badge${tone[d.decision[v]] || ""}">${esc(cap(d.decision[v]))}</span> The pre-registered decision, ${fy(v)}${other ? `; ${fy(other)} alone: ${esc(d.decision[other])}` : ""}.</p>
    <p class="note">${esc(d.headline)}</p>
    <div class="scroll"><table class="data india"><thead><tr><th scope="col">Test</th><th scope="col">Months the district’s register had none to issue: flagged</th><th scope="col">Well-stocked months: flagged</th><th scope="col" class="num">Rate ratio (95% CI)</th><th scope="col">Result, ${fy(v)}</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${shown.map((c) => `<p class="note">${esc(c)}</p>`).join("")}
    <details><summary>The rule, fixed before any result</summary><p class="note">${esc(d.rules.decision)}</p><p class="note">Flag: ${esc(d.rules.flag)}. Rules written ${esc(when(d.rules.written_at))}${d.rules.unchanged ? ", and the file is unchanged since" : ""}.</p></details>
    ${rest.length ? `<details><summary>${plural(rest.length, "more caveat")}</summary><ul class="caveats">${rest.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></details>` : ""}
    <p class="note">Data: <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.dataset)}</a>, ${esc(s.publisher)}, fetched ${esc(s.fetched)}. ${esc(s.attribution)} ${esc(s.licence)}</p>`;
  $("#india").hidden = false;
}
async function loadIndia() {
  try { renderIndia(await get("/api/real/india")); } catch (e) { /* no cached HMIS check: no panel */ }
}
function renderBench() {   // the app's own forecast against Google's TimesFM, from /api/google's row: run offline, never at runtime
  const r = (state.google?.services || []).find((x) => x.service.startsWith("TimesFM"));
  if (!r) return;
  $("#bench").innerHTML = `<strong>Forecast benchmark: the app’s ETS against Google’s TimesFM.</strong> The app forecasts 14-day demand from diagnoses with a damped-trend ETS. ${esc(r.detail.replace(/ x /g, " × "))}${r.as_of ? ` Queries run ${when(r.as_of)}.` : ""}`;
  $("#bench").hidden = false;
}

async function init() {   // the first load, and every change of network
  const request = ++state.request, busy = ["#signalList", "#detail", "#moves", "#careBoard", "#national", "#ccBody", "#ccMap"].map($);
  state.detailRequest++;
  stopReplay();
  clearTimeout(pending);
  ["#day", "#truth", "#play", "#district"].forEach((s) => { $(s).disabled = true; });
  const kind = state.net.startsWith("whatif:") ? "whatif" : state.net;
  document.querySelectorAll("#netSwitch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.net === kind)));
  busy.forEach((el) => fading(el, true));
  state.cache.clear();
  state.side.clear();
  // a new network opens on the district of its opening signal, with the investigation on its evidence
  Object.assign(state, { dayData: null, sel: null, prevPhantom: null, prevDay: null, page: 0, wh: undefined, detailTab: "evidence" });
  try {
    const meta = await get(api("/api/meta"));
    if (state.request !== request) return;   // a later switch of network took over
    state.meta = meta;
    setClock(meta.clock);
    $("#play span").textContent = playLabel(false);
    $("#replayLabel").textContent = meta.live ? "Simulated live feed" : meta.scenario?.whatif ? "What-if replay" : "Simulation replay";
    $("#mapTag").textContent = meta.real_sites ? "Real public PHC locations (OpenStreetMap); synthetic records" : "Synthetic PHC locations";
    $("#mapPost").hidden = !meta.real_sites;
    $("#guideNote").textContent = `${meta.live ? `A simulated live feed of ${meta.days} days: the first ${meta.live.history} arrive at the start, the rest one day at a time.`
      : `A ${meta.scenario?.whatif ? "what-if" : meta.scenario ? "scripted" : "synthetic"} replay of ${meta.days} days.`} Turn on “Show what the simulator hid” to see the true shelves, beds and staff.`;
    // real sites carry their OpenStreetMap credit on every tab, not only under the map
    $("#grammarNote").textContent = `${meta.real_sites ? `Locations: ${meta.sites_note} ` : ""}Rulebook ${meta.grammar}: ${meta.grammar_note}`;
    const states = [...new Set(meta.facilities.map((f) => f.st))], whs = [...new Set(meta.facilities.map((f) => f.wh))];
    const many = states.length > 1, count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    $("#networkSize").textContent = `${meta.facilities.length} PHCs, ${many ? count(states.length, "state") : count(whs.length, "district")}, ${meta.drugs.length} medicines`;
    $("#stateFilter").setAttribute("aria-label", many ? "Filter by state" : "Filter by district");
    $("#stateFilter").innerHTML = `<option value="">${many ? "All states" : "All districts"}</option>` + (many
      ? states.map((s) => `<option value="${esc(s)}">${stateName(s)}</option>`)
      : whs.map((w) => `<option value="${esc(w)}">${place(meta.facilities.findIndex((f) => f.wh === w)).wh}</option>`)).join("");
    renderContext();
    buildGrid();
    const s = meta.start;
    state.sel = { f: s.f, j: s.j };
    state.fresh = true;
    await show(s.day);
    if (state.request !== request + 1) return;
    busy.forEach((el) => fading(el, false));
    ["#day", "#truth", "#play"].forEach((s) => { $(s).disabled = false; });
    if (!$("#proof").children.length) await Promise.all([loadProof(), loadReal(), loadIndia()]);
  } catch {
    busy.forEach((el) => fading(el, false));
    showError("We couldn’t connect to the network. Check your connection and try again.");
    $("#signalList").setAttribute("aria-busy", "false");
    $("#detail").setAttribute("aria-busy", "false");
    $("#signalList").innerHTML = '<div class="empty-state"><strong>Network unavailable</strong><p>Use Try again above to reconnect.</p></div>';
    $("#detail").innerHTML = '<div class="empty-state"><strong>No signal loaded</strong><p>Investigation details will appear when the network connects.</p></div>';
  } finally { document.body.classList.remove("intro"); }
}
async function loadProof() {
  try { proof(await get("/api/eval")); }
  catch {
    $("#triageNote").innerHTML = 'Performance data could not load. <button class="button" id="retryProof">Try again</button>';
    $("#retryProof").addEventListener("click", loadProof);
  }
}
let pending;
$("#day").addEventListener("input", (e) => {
  stopReplay();
  const t = +e.target.value;
  setDay(t);
  clearTimeout(pending);
  pending = setTimeout(() => show(t), 100);
});
$("#truth").addEventListener("change", (e) => {
  stopReplay();
  clearTimeout(pending);
  state.truth = e.target.checked;
  show(+$("#day").value);
});
$("#play").addEventListener("click", () => {
  clearTimeout(pending);
  if (state.timer) return stopReplay();
  $("#play span").textContent = playLabel(true);
  $("#play").setAttribute("aria-pressed", "true");
  const live = Boolean(state.meta.live);   // a replay steps through days it has; the live feed sends the next one
  const step = async () => {
    if (live) await feedStep();
    else if (state.day >= state.meta.clock) return stopReplay();
    else await show(state.day + 1);
    if (state.timer) state.timer = setTimeout(step, live ? 400 : 700);
  };
  state.timer = setTimeout(step, live ? 0 : 700);
});
document.querySelectorAll("#netSwitch button").forEach((b) => b.addEventListener("click", () => {
  if (b.dataset.net === state.net) return;
  if (state.saving || state.rec) { notify("Finish the shelf check before changing network."); return; }
  if (b.dataset.net === "whatif") openWhatif();
  else switchNet(b.dataset.net);
}));
$("#context").addEventListener("click", (e) => {   // the story's days, and the live feed's buttons
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.day) { stopReplay(); clearTimeout(pending); show(+b.dataset.day); }
  else if (b.id === "jumpFeed") jumpFeed(b);
  else if (b.id === "paperDay") openIntake();
  else if (b.id === "restartFeed") askRestart();
  else if (b.id === "restartYes") restartFeed(b);
  else if (b.id === "restartNo") { renderContext(); $("#restartFeed").focus(); }
});
$("#whatifForm").addEventListener("submit", buildWhatif);
$("#whatifForm").addEventListener("change", (e) => { if (["world", "event"].includes(e.target.name)) wiFields(); });
$("#whatifMenu").addEventListener("click", (e) => { const b = e.target.closest("[data-key]"); if (b) openWorld(b.dataset.key); });
$("#closeWhatif").addEventListener("click", () => $("#whatif").close());
$("#closeIntake").addEventListener("click", () => $("#intake").close());
$("#intakeBody").addEventListener("submit", (e) => { e.preventDefault(); intakeRead(false); });
$("#intakeBody").addEventListener("change", (e) => {
  if (e.target.name === "files") useFiles(e.target);
  else if (e.target.dataset.reg && ik.stage === "check") ikEdit(e.target);
});
$("#intakeBody").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "ikSample") useSample();
  else if (b.id === "ikRecorded") intakeRead(true);
  else if (b.id === "ikConfirm") intakeConfirm(b);
  else if (b.id === "ikSkip") $("#ikConfirm").focus();
  else if (b.id === "ikBack" || b.id === "ikAgain") intakePick();
  else if (b.id === "ikOpen") {   // the PHC's own investigation, on the day it reported
    $("#intake").close();
    ik.stage = "pick";
    await show(+b.dataset.t);
    openDetail(urgent([+b.dataset.f], [...state.meta.drugs.keys()]));
  }
});
["#search", "#stateFilter", "#statusFilter"].forEach((s) => $(s).addEventListener(s === "#search" ? "input" : "change", () => { state.page = 0; state.animateList = true; renderSignals(); }));
document.querySelectorAll(".metric").forEach((m) => m.addEventListener("click", () => {   // each count is also a filter
  $("#statusFilter").value = $("#statusFilter").value === m.dataset.status ? "" : m.dataset.status;
  state.page = 0;
  state.animateList = true;
  renderSignals();
}));
$("#signalList").addEventListener("click", (e) => {
  const button = e.target.closest("[data-f]");
  if (button) select(+button.dataset.f, +button.dataset.j, true);
  if (e.target.id === "clearFilters") {
    ["#search", "#stateFilter", "#statusFilter"].forEach((s) => { $(s).value = ""; });
    state.page = 0;
    state.animateList = true;
    renderSignals();
    $("#search").focus();
  }
});
$("#prevPage").addEventListener("click", () => { state.page--; state.animateList = true; renderSignals(); });
$("#nextPage").addEventListener("click", () => { state.page++; state.animateList = true; renderSignals(); });
document.querySelectorAll("[data-display]").forEach((b) => b.addEventListener("click", () => {
  state.display = b.dataset.display;
  document.querySelectorAll("[data-display]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  state.animateList = true;
  renderSignals();
  if (state.display === "matrix" && !calm()) {   // the matrix fills in square by square
    $("#grid").classList.add("cascade");
    setTimeout(() => $("#grid").classList.remove("cascade"), 1200);
  }
}));
$("#retry").addEventListener("click", async () => {
  $("#retry").disabled = true;
  try { if (state.dayData) await show(state.day); else await init(); }
  finally { $("#retry").disabled = false; }
});
$("#help").addEventListener("click", () => $("#guide").showModal());
const closeGuide = () => { $("#guide").close(); if (location.hash === "#guide") location.hash = "command"; };
$("#closeGuide").addEventListener("click", closeGuide);
$("#startExploring").addEventListener("click", closeGuide);
$("#guide").addEventListener("cancel", () => { if (location.hash === "#guide") location.hash = "command"; });
$("#district").addEventListener("change", (e) => { state.wh = e.target.value || null; refreshCommand(); });
$("#ccBody").addEventListener("click", (e) => {   // a medicine: its most urgent PHC in the district
  const b = e.target.closest("[data-j]");
  if (b) openDetail(urgent([...state.meta.facilities.keys()].filter((f) => !state.wh || state.meta.facilities[f].wh === state.wh), [+b.dataset.j]));
});
const openPin = (e) => {   // a PHC on the map: its most urgent medicine
  const g = e.target.closest(".pin");
  if (g && (e.type === "click" || e.key === "Enter" || e.key === " ")) { e.preventDefault(); openDetail(urgent([+g.dataset.f], [...state.meta.drugs.keys()])); }
};
$("#ccMap").addEventListener("click", openPin);
$("#ccMap").addEventListener("keydown", openPin);
$("#stack").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) openService(b.dataset.chip); });
$("#closeSvc").addEventListener("click", () => $("#svc").close());
$("#svc").addEventListener("close", () => $(`#stack .chip[data-chip="${CSS.escape($("#svc").dataset.chip || "")}"]`)?.focus());
addEventListener("hashchange", navigate);
navigate();
renderStack();
loadGoogle();
init();
