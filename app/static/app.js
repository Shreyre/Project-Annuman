const $ = (s) => document.querySelector(s);
const NETS = ["demo", "kerala", "live"];
const asked = new URLSearchParams(location.search).get("net");
const state = { net: NETS.includes(asked) ? asked : "demo", meta: null, day: 0, truth: false, sel: null, timer: null, rec: null,
  cache: new Map(), side: new Map(), dayData: null, cells: [], byKey: new Map(), prevPhantom: null, prevDay: null, fresh: false };
const REGIME = { OK: ["ok", "Stocked"], SCARCE: ["scarce", "Running short"], OUT: ["out", "Empty"] };
const WORD = { tab: "tablets", cap: "capsules", sachet: "sachets" };
const CADRE = { MO: "Medical officer", SN: "Staff nurse", PH: "Pharmacist", LT: "Lab technician" };
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
    return `<tr class="${selected ? "selected" : ""}" data-f="${c.f}" data-j="${c.j}" style="--i:${i}"><td><button class="signal-link" data-f="${c.f}" data-j="${c.j}" aria-pressed="${selected}" aria-label="Investigate ${p.phc}, ${p.wh}, ${p.st}, ${drugName(c.j)}">${drugName(c.j)}</button><small>${p.phc}, ${p.wh}, ${p.st}${c.as_of === undefined ? "" : `. Last report: day ${c.as_of}`}</small></td><td><span class="badge ${cls}${c.phantom ? " phantom" : ""}">${c.phantom ? "Hidden stock-out" : word}</span>${c.confirmed ? '<small>Shelf check recorded</small>' : ''}</td><td class="register-col num"><span class="ledger${c.phantom ? " struck" : ""}">${fmt(c.book)}</span><small>${unitOf(c.j)}</small></td><td class="num">${c.shadow === null ? '<small>No use yet</small>' : `<span class="days">${fmt(c.shadow)}</span><span class="cover ${cls}" style="--v:${Math.min(100, Math.round((100 * c.shadow) / 30))}%" aria-hidden="true"></span>`}${c.true === undefined ? '' : `<small>Actual: ${fmt(c.true)} ${unitOf(c.j)}</small>`}</td></tr>`;
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

const VIEWS = {
  overview: ["Overview", "Network overview", "The register says it’s on the shelf. The care says otherwise."],
  transfers: ["Redistribution", "Put supply where it’s needed", "Prioritize transfers and escalate the gaps that need a wider response."],
  care: ["Beds & staff", "Beds and staff", "Where a bed is free, and which attendance marks the care record does not back."],
  "national-view": ["National view", "See the bigger picture", "Connect state-level signals while keeping care records local."],
  validation: ["Performance", "Confidence, backed by evidence", "What the model catches, how early, and where it falls short."]
};
function navigate() {
  let key = location.hash.slice(1) || "overview";
  if (key === "guide") { $("#guide").showModal(); return; }
  if (!VIEWS[key]) key = "overview";
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== key; });
  document.querySelectorAll("[data-view]").forEach((a) => { if (a.dataset.view === key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  const [label, title, description] = VIEWS[key];
  $("#pageTitle").textContent = title;
  $("#pageDescription").textContent = description;
  document.title = `${label} | Anumaan`;
  hideTip();
}

function drugName(j) {   // names come from the (Gemini-compiled) rulebook, so escape them
  const [name, strength] = state.meta.drugs[j].name.split(/_(?=[^_]+$)/);
  const base = name.split("_").map((w) => (w.length <= 3 ? w.toUpperCase() : w)).join(" ");   // ifa -> IFA, ors -> ORS
  return esc((base[0].toUpperCase() + base.slice(1)) + (strength && /^\d/.test(strength) ? ` ${strength}` : ""));
}
const drugOf = (name) => state.meta.drugs.findIndex((d) => d.name === name);
const unitOf = (j) => WORD[state.meta.drugs[j].unit] || "units";
function place(f) {   // a scenario names its real state and districts; the demo network is numbered
  const fac = state.meta.facilities[f], [, w, p] = fac.id.split("-"), named = state.meta.names.warehouses?.[fac.wh];
  return { st: stateName(fac.st), wh: named ? esc(named) : `Warehouse\u00a0${String.fromCharCode(65 + +w.slice(1))}`, phc: `PHC\u00a0${+p.slice(1) + 1}` };   // never split "PHC 6" across lines
}
const facOf = (id) => state.meta.facilities.findIndex((x) => x.id === id);
const stateName = (id) => (state.meta.names.states?.[id] ? esc(state.meta.names.states[id]) : `State\u00a0${+id.slice(1) + 1}`);
const api = (path) => `${path}${path.includes("?") ? "&" : "?"}net=${state.net}`;   // every call names its network
const road = (minutes) => (state.meta.real_roads ? `${minutes} min by road` : `about ${minutes} min by road`);
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
  const rails = ["#moves", "#careBoard", "#national"].map($);
  rails.forEach((el) => fading(el, true));
  try {
  const data = await cached(state.cache, `${t}|${truth}`, api(`/api/day/${t}${truth ? "?truth=true" : ""}`));
  if (state.request !== request) return;
  state.dayData = data;
  state.dayTruth = truth;
  $("#loadError").hidden = true;
  paintGrid(data);
  headline(data, state.fresh);
  const detail = state.sel ? renderDetail() : Promise.resolve();
  const [plan, board, nat] = await Promise.all([cached(state.side, `plan|${t}`, api(`/api/plan?t=${t}`)),
    cached(state.side, `care|${t}|${truth}`, api(`/api/care?t=${t}${truth ? "&truth=true" : ""}`)),
    cached(state.side, `nat|${t}`, api(`/api/national?t=${t}`))]);
  if (state.request !== request) return;
  renderMoves(plan);
  renderCare(board);
  renderNational(nat);
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
  return {
    LOCAL: `${p.wh} is being supplied by the state${filled} and demand is normal, so the problem is at this PHC.`,
    WAREHOUSE: `The state has stopped filling ${p.wh}'s indents for this medicine${filled}, while other warehouses in ${p.st} are still supplied.`,
    "STATE-PROCUREMENT": `The state has stopped filling this medicine's indents at ${c.starved} of ${p.st}'s ${c.warehouses} warehouses. One warehouse failing on its own would not look like this.`,
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
  // four narrow columns so the table fits the side panel without scrolling: posts sit under the role
  const rows = fa.staff.map((r) => `<tr><th scope="row">${CADRE[r.cadre]}<small>${r.in_position} of ${r.sanctioned} in post</small></th>
    <td>${r.in_position ? (r.marked_present ? "Present" : "Absent") : "n/a"}</td><td>${atWork(r)}</td>
    ${r.true_present === undefined ? "" : `<td>${r.true_present ? "At work" : "Away"}</td>`}</tr>`).join("");
  return `<section><h3>${p.phc} today</h3>
    <div class="beds" aria-hidden="true">${beds}</div>
    <p class="why">Beds: <strong>${b.occupied} of ${b.capacity}</strong> occupied, counted from admissions and recorded discharges${b.pressure ? ". Every bed is taken" : ""}.${early}${truth}</p>
    <table class="data staff"><thead><tr><th scope="col">Staff</th><th scope="col">Attendance</th><th scope="col">At work, from care records</th>${fa.staff[0].true_present === undefined ? "" : `<th scope="col">Ground truth</th>`}</tr></thead><tbody>${rows}</tbody></table>
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
  const truth = c.true === undefined ? "" :
    `<p class="truthline"><strong>Ground truth:</strong> ${fmt(c.true)} ${unit} on the shelf (${c.true_cover} days of use).</p>`;
  const alarm = !c.alarm ? `<h3>No alarm</h3><p class="why">Care at this PHC matches what the diagnoses call for, and deliveries minus dispensing leave more than ${state.meta.low} days of use.</p>` : `
    <h3>Alarm since day ${c.onset}</h3>
    ${c.by_stock ? `<p class="why">Care still looks normal, but deliveries minus dispensing leave only about ${c.shadow} days of use. The shelf is running down before anyone has started rationing.</p>` : ""}
    <p class="why">${evidence(c, p)}</p>
    ${tree(c, p)}
    <p class="action ${["LOCAL", "WAREHOUSE", "DEMAND-SURGE"].includes(c.level) ? "local" : ""}"><strong>Suggested next step:</strong> ${esc(state.meta.actions[c.level])}. Where it broke is read from the warehouse ledger; confirm with the district store before escalating.</p>
    <h3>Brief for the district officer</h3>
    <p class="why">Gemini writes it from the evidence above, in the officer's language.</p>
    <p class="brief-ask"><select id="briefLang" aria-label="Language of the brief"><option value="en">English</option><option value="or" lang="or">ଓଡ଼ିଆ (Odia)</option>
      <option value="hi" lang="hi">हिन्दी (Hindi)</option><option value="ml" lang="ml">മലയാളം (Malayalam)</option></select>
      <button type="button" class="button" id="briefBtn">Write brief</button></p>
    <div id="briefOut" role="status"></div>`;
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
    <section data-pane="evidence">${alarm}</section>
    <section class="confirm" data-pane="evidence">
      <h3>Ask the pharmacist</h3>
      <p class="why">Ask them to check the shelf. Their answer updates the estimate.</p>
      ${asked}
      <div class="buttons">
        <button type="button" class="yes" data-answer="empty">Yes, it's finished</button>
        <button type="button" data-answer="available">No, we have it</button>
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
  } catch (e) {
    if (state.detailRequest !== request) return;
    panel.setAttribute("aria-busy", "false");
    panel.classList.remove("updating");
    panel.innerHTML = '<div class="empty-state"><strong>Investigation unavailable</strong><p>We couldn’t load the care evidence. Check your connection and try again.</p><button class="button" id="retryDetail">Retry investigation</button></div>';
    $("#retryDetail").addEventListener("click", renderDetail);
  }
}

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
      <span class="qty"><span class="vh">Send</span><b>${m.courses}</b> courses<small>${fmt(m.units)} ${unitOf(j)}</small></span>
      <span class="med">${drugName(j)}</span>
      <span class="route"><span class="vh">from</span>${end(m.from_fac)}<span class="road">${road(m.minutes)}</span><span class="vh">to</span>${end(m.to_fac)}</span>
      <span class="st">${place(facOf(m.to_fac)).st}${m.approved ? '<small class="done">Order approved</small>'
        : `<button type="button" class="button" data-approve="${esc(JSON.stringify({ from_fac: m.from_fac, to_fac: m.to_fac, drug: m.drug }))}">Approve</button>`}</span></li>`;
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
  b.disabled = true;
  try {
    const r = await fetch(api("/api/approve"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ t: state.day, ...JSON.parse(b.dataset.approve) }) });
    if (!r.ok) throw new Error(`approve answered ${r.status}`);
    const o = await r.json();
    b.outerHTML = `<small class="done">Order ${esc(o.indent_id)} approved</small>`;
    state.side.delete(`plan|${o.day}`);   // the cached plan does not know about the approval yet
    const from = place(facOf(o.from)), to = place(facOf(o.to));
    notify(`Issue order ${o.indent_id} is in the ledger: ${o.courses} courses from ${from.phc}, ${from.wh} to ${to.phc}, ${to.wh}.`);
  } catch (e) {
    b.disabled = false;
    notify("That transfer could not be approved. Try again.");
  }
}

async function writeBrief() {   // Gemini turns the evidence above into a brief in the officer's language
  const { f, j } = state.sel, t = state.day, lang = $("#briefLang").value, btn = $("#briefBtn"), out = $("#briefOut");
  btn.disabled = true;
  out.textContent = "Gemini is writing the brief…";
  try {
    const b = await get(api(`/api/brief/${f}/${j}?t=${t}&language=${lang}`));
    if (state.sel.f !== f || state.sel.j !== j || state.day !== t) return;
    out.innerHTML = `<p class="why" lang="${lang}">${esc(b.summary)}</p><p class="action" lang="${lang}"><strong>Next step:</strong> ${esc(b.next_step)}</p>`;
  } catch (e) {
    out.textContent = "Gemini could not write the brief just now. The evidence above still stands.";
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
  $("#proof").innerHTML = `<thead><tr><th scope="col">Method</th><th scope="col">Stock-outs of 4+ days caught</th><th scope="col">Warned before the shelf emptied</th><th scope="col">Typical warning</th><th scope="col" class="num">False alarms per medicine per year</th></tr></thead>
    <tbody>${rows.map(([n, r], k) => `<tr${k ? "" : ' class="lead"'}><th scope="row">${n}</th>${meter(r.recall_4d)}${meter(r.early)}<td>${lead(r.median_lead)}</td><td class="num">${r.false_per_series_year.toFixed(2)}</td></tr>`).join("")}</tbody>`;
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
    ${r.refer ? `Nearest free bed: ${where(r.refer.f, false)}, ${road(r.refer.minutes)}, ${r.refer.free} free.` : `No PHC within ${b.refer_minutes} minutes has a free bed. Refer to the community health centre.`}${r.true_occupied === undefined ? "" : ` <em>Ground truth: ${r.true_occupied} occupied.</em>`}</span></li>`).join("");
  const marks = checks.slice(0, 8).map(([r, x]) => `<li><span class="tag">Check attendance</span><span>${where(r.f)}: ${esc(CADRE[x.cadre].toLowerCase())} marked present, but ${x.acts} of about ${Math.round(x.expected)} expected tasks are in the care record${x.verify_7d > 1 ? `. Flagged on ${x.verify_7d} of the last 7 days` : ""}.${x.true_present === undefined ? "" : ` <em>Ground truth: ${x.true_present ? "at work" : "away"}.</em>`}</span></li>`).join("");
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

function renderContext() {   // what is different about this network, shown above every view
  const m = state.meta, box = $("#context");
  box.hidden = !(m.scenario || m.live);
  if (m.scenario) {
    const s = m.scenario;
    const links = s.sources.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.label)}</a>`).join(" and ");
    box.innerHTML = `<div><strong>${esc(s.title)}</strong><p>${esc(s.script)}</p><p class="quiet">${esc(s.event)} Reports: ${links}.</p><p class="quiet">${esc(s.note)}</p></div>`;
  } else if (m.live) {
    const s = m.live, via = { "Pub/Sub": " The reports travel through Google Cloud Pub/Sub.", direct: " Pub/Sub is not configured on this copy, so the reports are applied directly." }[s.via] || " Press Start feed to send the next day’s reports.";
    const log = s.log.map((e) => { const p = place(e.f); return `<li>Day ${e.day}, ${p.phc}, ${p.wh}, ${p.st}: ${[e.raised.length ? `new alarm on ${e.raised.map(drugName).join(", ")}` : "", e.cleared.length ? `alarm cleared on ${e.cleared.map(drugName).join(", ")}` : ""].filter(Boolean).join("; ")}</li>`; }).join("");
    box.innerHTML = `<div><strong>Live feed: day ${s.day} of ${s.last}</strong><p>A simulated network, not a real one: ${m.facilities.length} made-up PHCs in ${new Set(m.facilities.map((f) => f.st)).size} made-up states, played forward by the simulator. No real PHC is connected. Each PHC’s day arrives as one message: its diagnoses, dispensing slips, register balances, admissions and attendance. ${s.reported} of ${s.phcs} PHCs have reported day ${s.day}${s.seconds === null ? "" : `, and their estimates were updated ${s.seconds} seconds after the reports were sent`}.${via}</p>${log ? `<ul class="feed-log" aria-label="Latest changes">${log}</ul>` : ""}</div><button type="button" class="button" id="restartFeed">Restart feed</button>`;
    $("#restartFeed").addEventListener("click", restartFeed);
  }
}

function setClock(n) {   // the last day there is data for: fixed in a replay, moving in the live feed
  state.meta.clock = n;
  $("#day").max = n;
  $("#dayMax").textContent = n;
}

async function feedStep() {   // send the next day's reports, wait for them to land, then show that day
  try {
    const r = await fetch("/api/live/step", { method: "POST" });
    if (r.status === 409) { stopReplay(); return notify("The feed has reached its last day. Restart it to run again."); }
    if (!r.ok) throw new Error(`step answered ${r.status}`);
    const sent = await r.json();
    let s = await get("/api/live");
    for (let i = 0; i < 24 && !(s.day >= sent.day && s.complete); i++) {   // a Pub/Sub push lands within a second or two
      await new Promise((done) => setTimeout(done, 250));
      s = await get("/api/live");
    }
    if (state.net !== "live") return;
    state.meta.live = s;
    setClock(s.day);
    renderContext();
    state.cache.clear();
    state.side.clear();
    await show(s.day);
  } catch (e) {
    stopReplay();
    notify("The feed stopped: the reports could not be sent. Try again.");
  }
}

async function restartFeed(e) {
  e.currentTarget.disabled = true;
  stopReplay();
  try {
    const r = await fetch("/api/live/reset", { method: "POST" });
    if (!r.ok) throw new Error(`reset answered ${r.status}`);
    await init();
    notify("The feed is back at its first day.");
  } catch (err) {
    notify("The feed could not be restarted. Try again.");
    if ($("#restartFeed")) $("#restartFeed").disabled = false;
  }
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
const month = (m) => new Date(`${m}-01T00:00:00Z`).toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" });

function renderReal(d) {   // the premise, asked of real dispensing records: England's, because India publishes none
  const most = (x, s) => { const v = Math.max(...x.flagged); return v ? `<strong>${v} of ${s.icbs}</strong><small>${month(s.months[x.flagged.indexOf(v)])}</small>` : "None"; };
  const rows = d.shortages.map((s) => {
    const first = [...s.cut_short.wide_months, ...s.substituting.wide_months].sort()[0], lead = s.months.indexOf(s.notice_month) - s.months.indexOf(first);
    return `<tr><th scope="row">${esc(s.name)}</th><td>${month(s.notice_month)}</td><td>${most(s.substituting, s)}</td><td>${most(s.cut_short, s)}</td>
      <td>${first ? `${month(first)}<small>${lead > 0 ? `${lead} ${lead === 1 ? "month" : "months"} before the notice` : lead ? "after the notice" : "the month of the notice"}</small>` : "Never"}</td><td>${realChart(s)}</td></tr>`;
  }).join("");
  const quiet = Math.max(...d.placebos.flatMap((p) => p.substituting.flagged)), cut = d.placebos.reduce((n, p) => n + p.cut_short.wide_months.length, 0);
  const swide = d.placebos.some((p) => p.substituting.wide_months.length);
  $("#realBody").innerHTML = `<p class="note">India publishes no dispensing records at this grain. England does, so this asks one thing of real data: when a medicine is officially declared short, do the fingerprints Anumaan reads show up in routine dispensing, in many areas at once? An area is one of England’s ${d.shortages[0].icbs} integrated care boards. It is flagged when it moves more than ${d.rule.z} robust standard deviations from its own ${d.rule.baseline_months} months before the first official notice.</p>
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

async function init() {   // the first load, and every change of network
  const request = ++state.request, busy = ["#signalList", "#detail", "#moves", "#careBoard", "#national"].map($);
  state.detailRequest++;
  stopReplay();
  clearTimeout(pending);
  ["#day", "#truth", "#play"].forEach((s) => { $(s).disabled = true; });
  document.querySelectorAll("#netSwitch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.net === state.net)));
  busy.forEach((el) => fading(el, true));
  state.cache.clear();
  state.side.clear();
  Object.assign(state, { dayData: null, sel: null, prevPhantom: null, prevDay: null, page: 0 });
  try {
    const meta = await get(api("/api/meta"));
    if (state.request !== request) return;   // a later switch of network took over
    state.meta = meta;
    setClock(meta.clock);
    $("#play span").textContent = playLabel(false);
    $("#grammarNote").textContent = `Rulebook ${meta.grammar}: ${meta.grammar_note}`;
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
    if (!$("#proof").children.length) await Promise.all([loadProof(), loadReal()]);
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
  state.net = b.dataset.net;
  history.replaceState(null, "", `${state.net === "demo" ? location.pathname : `?net=${state.net}`}${location.hash}`);
  init();
}));
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
const closeGuide = () => { $("#guide").close(); if (location.hash === "#guide") location.hash = "overview"; };
$("#closeGuide").addEventListener("click", closeGuide);
$("#startExploring").addEventListener("click", closeGuide);
$("#guide").addEventListener("cancel", () => { if (location.hash === "#guide") location.hash = "overview"; });
addEventListener("hashchange", navigate);
navigate();
init();
