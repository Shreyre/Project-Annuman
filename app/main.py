"""Anumaan demo service: runs SYNTHETIC PHC networks through the filter and serves the
Counter Truth, beds and staff, forecast, redistribution and national views. One container:
API + static UI.

    uvicorn app.main:app --reload

Three networks, chosen with ?net= on any call:
  demo    the held-out simulator run the README's results are reported on
  kerala  a scenario replay: the same simulator, its failures scripted from a reported event
  live    the same pipeline fed one PHC's day at a time through /api/ingest (Pub/Sub push)
and what-if worlds (anumaan/whatif.py), ?net=whatif:..., built by POST /api/whatif.
Only the first is built at start-up; the others on first use (ANUMAAN_WARM=1 builds all,
and the what-if menu's picks).
"""
import base64
import hmac
import json
import logging
import os
import threading
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from functools import cached_property, wraps
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from anumaan import care as CARE, crg as G, federation as FED, feeds, filter as FL, forecast as FC
from anumaan import intake, planner as PL, realcheck, realcheck_india, scenario, sim, triage, voice, whatif
from anumaan.evaluate import evaluate

THR = 0.7
HORIZON = 14
WARMUP = 30          # a PHC's prescribing rate and register trust are learned from its first 30 days
REFER_MINUTES = 90   # how far a full PHC may send a patient for a bed
AUDIT_DAYS = 90      # a PHC's attendance marking is rated over its last 90 days
SEED = int(os.environ.get("ANUMAAN_SEED", 5))   # a held-out seed: never used for tuning
# The live feed: TOPIC set -> reports go out through Pub/Sub and come back by push to /api/ingest,
# which needs INGEST_TOKEN; unset -> the same messages are applied in-process.
TOPIC = os.environ.get("ANUMAAN_PUBSUB_TOPIC")
INGEST_TOKEN = os.environ.get("ANUMAAN_INGEST_TOKEN")
LIVE_FROM = 60       # days of history the live network starts with, as a state's back-load would give it
# The public demo link lets anyone trigger a paid Gemini call (voice, brief, read-aloud or register
# photos), so cap them per day.
# ponytail: per-process counter; with several Cloud Run instances each gets its own cap
GEMINI_CAP = int(os.environ.get("ANUMAAN_GEMINI_DAILY_CAP", 200))
gemini_used, briefs = {}, {}
# Briefs read aloud, keyed like briefs. ponytail: the last CLIPS only; a WAV is about 48 KB a second of speech
clips, CLIPS = {}, 20
# Approved transfers, keyed (net, day, from, to, drug). ponytail: in memory and lost on restart;
# a real deployment posts each order to the state's DVDMS instead
ledger = {}
USUAL = 28           # the district screen's "usual" diagnoses: the mean of the 28 days before
FOOTFALL = f"diagnoses recorded for the {len(G.load()['conditions'])} conditions Anumaan tracks, not every visit"
IST = timezone(timedelta(hours=5, minutes=30))
STARTED = time.time()    # this instance's start, for /api/google
seen = {}                # "push" / "publish": when this process last had a Pub/Sub push / published a day


def _spend():
    today = date.today()
    if gemini_used.get(today, 0) >= GEMINI_CAP:
        raise HTTPException(429, "Gemini calls are used up for today on this demo. The buttons still work.")
    gemini_used[today] = gemini_used.get(today, 0) + 1


class Replay:
    """Everything the UI can ask for about one network. The filter is causal, so the view
    at day t only ever reflects data up to day t."""
    live = False
    real_sites = False      # coords are real public PHC locations (OpenStreetMap), not SYNTHETIC points

    def __init__(self, run, seed, care=None, coords=None, names=None, upto=None, open_day=None):
        self.run, self.seed, self.names, self.open_day = run, seed, names or {}, open_day
        self.drugs = P = list(run.ix["primaries"])
        T, F, J = run.book.shape[0], len(run.facilities), len(P)
        self.days, self.whs, self.lock = T, sorted(set(run.wh)), threading.RLock()
        self.upto = np.full(F, T - 1) if upto is None else upto      # the last day each PHC has reported
        self.obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
        self.care = care or CARE.simulate(run, seed)
        self.coords = PL.synthetic_coords(run, seed) if coords is None else coords
        self.post, self.cover = np.zeros((T, F, J, 3)), np.zeros((T, F, J))
        self.use, self.onset = np.zeros((T, F, J)), np.full((T, F, J), -1)
        self.skip, self.confirm = [None] * F, {}
        self.segs = {(f, j): [] for f in range(F) for j in range(J)}
        # views onto self.post / self.cover keyed by drug index, as the planner expects; refilter writes in place
        self.post_d = {(f, d): self.post[:, f, j] for f in range(F) for j, d in enumerate(P)}
        self.cover_d = {(f, d): self.cover[:, f, j] for f in range(F) for j, d in enumerate(P)}
        self.version, self._views = 0, {}
        for f in range(F):
            self.refresh(f)

    def refresh(self, f):
        """Rebuild one PHC's series from the records it has sent so far."""
        obs, P = self.obs, self.drugs
        given = sum(obs[k][:, f, P].sum(1) for k in G.CATS)
        self.skip[f] = FL.entry_gaps(given, obs["N"][:, f, P].sum(1))
        for j, d in enumerate(P):
            self.use[:, f, j] = FL.trailing_mean(obs["exp_units"][:, f, d])
            self.refilter(f, j)

    def refilter(self, f, j):
        run, obs, d, n = self.run, self.obs, self.drugs[j], int(self.upto[f]) + 1
        if n < WARMUP:
            return
        cats = np.stack([obs[k][:n, f, d] for k in G.CATS], 1)
        N, rec, book, exp = obs["N"][:n, f, d], run.receipts[:n, f, d], run.book[:n, f, d], obs["exp_units"][:n, f, d]
        skip, checks = self.skip[f][:n], self.confirm.get((f, j))
        rho, tau = FL.learn(N, cats, book, exp, rec, skip)
        p = FL.filter_series(N, cats, book, exp, rec, skip, tau=tau, confirm=checks, rho=rho)
        c = FL.shadow_cover(book, rec, obs["units"][:n, f, d], FL.trailing_mean(rho * exp), p, skip, checks)
        self.segs[(f, j)] = segs = FL.alarms(p, THR, c)
        on = np.full(n, -1)
        for s, e in segs:
            on[s + 1:e] = s       # an alarm needs 2 days: live from s+1
        # days a PHC has not reported yet carry its last known state
        self.post[:n, f, j], self.post[n:, f, j] = p, p[-1]
        self.cover[:n, f, j], self.cover[n:, f, j] = c, c[-1]
        self.onset[:n, f, j], self.onset[n:, f, j] = on, on[-1]

    @property
    def clock(self):
        """The latest day any PHC has reported: the last day of a replay."""
        return int(self.upto.max())

    def view(self, key, fn):
        """A network-wide view, rebuilt only once new records have arrived."""
        with self.lock:
            if self._views.get(key, (None,))[0] != self.version:
                self._views[key] = (self.version, fn())
            return self._views[key][1]

    fill = property(lambda self: self.view("fill", lambda: triage.fill_rate(self.run.wh_asked, self.run.wh_got, self.run.wh_posted)))
    lift = property(lambda self: self.view("lift", lambda: triage.surge_lift(self.obs["N"], self.run.st)))
    dlift = property(lambda self: self.view("dlift", lambda: triage.surge_lift(self.obs["N"], self.run.wh)))
    beds = property(lambda self: self.view("beds", lambda: CARE.beds(self.care)))
    staff = property(lambda self: self.view("staff", lambda: CARE.staff(self.care)))
    nodes = property(lambda self: self.view("nodes", lambda: [FED.StateNode(self.run, s) for s in sorted(set(self.run.st))]))

    @cached_property
    def metrics(self):
        return evaluate(self.run)

    @cached_property
    def mins(self):
        """[F, F] drive minutes between PHCs of one state; infinite across a state line."""
        st = np.array(self.run.st)
        return np.where(st[:, None] == st, PL.travel_minutes(self.coords), np.inf)

    @property
    def start(self):
        """Where the UI opens: the day with the most hidden stock-outs, on the one that broke
        furthest upstream, so the chain shows; a scenario names its day instead, and opens on its
        alarm that broke furthest upstream. A what-if opens where its story says."""
        if "whatif" in getattr(self, "about", {}):
            return self.about["whatif"]["start"]

        def find():
            F, J = len(self.run.facilities), len(self.drugs)
            if self.open_day is None:
                ph = np.array([[[self.phantom(t, f, j) for j in range(J)] for f in range(F)] for t in range(self.days)])
                t = int(ph.sum((1, 2)).argmax())
                hot = np.argwhere(ph[t])
            else:
                t = self.open_day
                hot = np.argwhere(self.onset[t] >= 0)
            lab = self.labels(t)
            rank = lambda f, j: (triage.LEVELS.index(lab[(f, j, int(self.onset[t, f, j]))]) % 4, self.post[t, f, j, 2])
            f0, j0 = max(hot.tolist(), key=lambda x: rank(*x), default=(0, 0))
            return dict(day=t, f=f0, j=j0)
        return self.view("start", find)

    def place(self, fid):
        """S0-W1-P5 -> the names the UI shows (app.js place()): State 1, Warehouse B, PHC 6."""
        s, w, p = fid.split("-")
        return dict(state=self.names.get("states", {}).get(s, f"State {int(s[1:]) + 1}"),
                    warehouse=self.names.get("warehouses", {}).get(f"{s}-{w}", f"Warehouse {chr(65 + int(w[1:]))}"),
                    phc=f"PHC {int(p[1:]) + 1}")

    def phantom(self, t, f, j):
        return self.onset[t, f, j] >= 0 and self.post[t, f, j, 2] >= THR and \
            self.run.book[t, f, self.drugs[j]] >= 7 * max(self.use[t, f, j], 1e-9)

    def labels(self, t):
        known = [(f, j, s) for (f, j), segs in self.segs.items() for s, _ in segs if s + 1 <= t]
        onsets = [(f, self.drugs[j], s) for f, j, s in known]      # triage indexes drugs, not columns
        return dict(zip(known, triage.classify(onsets, self.run.wh, self.run.st, self.fill, self.lift, t, self.dlift)))

    def cell(self, t, f, j, labels, truth):
        run, d = self.run, self.drugs[j]
        a = min(t, int(self.upto[f]))       # a PHC that has not reported day t shows its last report
        p, book, use = self.post[a, f, j], run.book[a, f, d], self.use[a, f, j]
        used = use > 0      # no diagnosis has called for it yet: days of use are undefined, not huge
        c = dict(f=f, j=j, book=round(book), cover=round(book / use, 1) if used else None,
                 shadow=round(float(self.cover[a, f, j]), 1) if used else None,
                 p=[round(float(x), 3) for x in p], regime=FL.REGIMES[int(p.argmax())],
                 alarm=bool(self.onset[a, f, j] >= 0), phantom=bool(self.phantom(a, f, j)),
                 confirmed=(self.confirm.get((f, j)) or {}).get(t))
        if a < t:
            c["as_of"] = a
        if c["alarm"]:
            s, w = int(self.onset[a, f, j]), self.whs.index(run.wh[f])
            fill = self.fill[t, w, d]
            mine = [k for k, x in enumerate(self.whs) if x.split("-")[0] == run.st[f]]
            c.update(onset=s, level=labels[(f, j, s)], action=triage.ACTION[labels[(f, j, s)]],
                     by_stock=bool(p[1] + p[2] < THR),   # live only because the shadow stock is low
                     fill=None if np.isnan(fill) else round(float(fill), 2), lift=round(float(self.lift[t, f, d]), 2),
                     district_lift=round(float(self.dlift[t, f, d]), 2),
                     # how many of the state's warehouses the state has stopped supplying with this medicine
                     starved=int(triage.starved(self.fill, s - triage.LOOK, t)[mine, d].sum()), warehouses=len(mine))
        if truth:
            c["true"] = round(run.true_stock[t, f, d])
            c["true_cover"] = round(run.true_stock[t, f, d] / max(run.true_use[t, f, d], 1e-9), 1)
        return c

    def board(self, t, truth=False):
        """Beds and staff across the network on day t. For a PHC whose count says every bed is
        taken: the nearest PHC in its state with a free bed, within REFER_MINUTES by road. For
        each cadre: today's attendance mark against the care record, and how many of the last
        7 days' marks the care record did not back. For the PHC: how often, over AUDIT_DAYS, it
        marked a cadre present on a day the care record shows it away. Footfall: see FOOTFALL."""
        c, b, s, F, dx = self.care, self.beds, self.staff, len(self.care.facilities), self.run.dx.sum(2)
        at = np.minimum(t, self.upto)       # a PHC that has not reported day t shows its last report
        away, marked = CARE.proxy_marking(c, max(CARE.WARMUP, t - AUDIT_DAYS + 1), t + 1, s)
        free = c.capacity - b["occupied"][at, np.arange(F)]
        rows = []
        for f in range(F):
            a = int(at[f])
            week, full, early = slice(max(a - 6, 0), a + 1), bool(b["pressure"][a, f]), b["early"][a, f]
            near = [g for g in np.argsort(self.mins[f])
                    if g != f and free[g] > 0 and self.mins[f, g] <= REFER_MINUTES] if full else []
            row = dict(f=f, capacity=int(c.capacity[f]), occupied=int(b["occupied"][a, f]), free=int(free[f]),
                       pressure=full, full_7d=int(b["pressure"][week, f].sum()),
                       footfall=dict(today=int(dx[a, f]), mean_7d=round(float(dx[week, f].mean()), 1)),
                       early_share_7d=None if np.isnan(early) else round(float(early), 2),
                       refer=dict(f=int(near[0]), minutes=round(float(self.mins[f, near[0]])),
                                  free=int(free[near[0]])) if near else None,
                       # of the days the care record showed a cadre away, how many the attendance feed marked present
                       audit=dict(away=int(away[f]), marked=int(marked[f])) if away[f] >= CARE.AUDIT_MIN else None,
                       staff=[dict(cadre=k, sanctioned=int(CARE.SANCTIONED[i]), in_position=int(c.in_position[f, i]),
                                   marked_present=bool(c.marked[a, f, i]), p_present=round(float(s["p_present"][a, f, i]), 2),
                                   acts=int(c.acts[a, f, i]), expected=round(float(s["expected"][a, f, i]), 1),
                                   verify=bool(s["verify"][a, f, i]), verify_7d=int(s["verify"][week, f, i].sum()))
                              for i, k in enumerate(CARE.CADRES)])
            if a < t:
                row["as_of"] = a
            if truth:
                row["true_occupied"] = int(c.occupancy[a, f])
                for i, x in enumerate(row["staff"]):
                    x["true_present"] = bool(c.present[a, f, i])
            rows.append(row)
        return dict(day=t, rows=rows, refer_minutes=REFER_MINUTES, audit_days=AUDIT_DAYS, footfall_is=FOOTFALL, summary=dict(
            beds=int(c.capacity.sum()), occupied=sum(r["occupied"] for r in rows), full=sum(r["pressure"] for r in rows),
            no_bed_near=sum(r["pressure"] and not r["refer"] for r in rows),
            verify=sum(x["verify"] for r in rows for x in r["staff"]),
            vacant=int(np.maximum(CARE.SANCTIONED - c.in_position, 0).sum()),
            footfall=dict(today=sum(r["footfall"]["today"] for r in rows),
                          mean_7d=round(float(sum(dx[max(a - 6, 0):a + 1, f].mean() for f, a in enumerate(at))), 1))))


class Live(Replay):
    """The same pipeline, fed one PHC's day at a time. `src` stands in for the PHCs' own
    systems: a SYNTHETIC run whose rows reach this network only as feed messages: the first
    `history` days at start-up, as a state's back-load would, and the rest through ingest()."""
    live = True

    def __init__(self, seed, history=LIVE_FROM):
        self.src = src = sim.simulate(seed=seed)
        c = CARE.simulate(src, seed)
        self.feed, self.history = feeds.stream(src, c), history
        T, F = src.book.shape[:2]
        blank = np.zeros_like
        run = sim.Run(src.ix, src.facilities, src.wh, src.st, blank(src.dx), [], [], blank(src.book), blank(src.receipts),
                      blank(src.wh_asked), blank(src.wh_got), np.full(src.wh_posted.shape, T),
                      src.true_stock, src.rate, src.true_use, src.ration_cover, src.episodes)   # truth: for the ground-truth switch only
        care = CARE.Care(c.facilities, c.capacity, [], [], c.in_position, blank(c.marked), blank(c.exposure),
                         blank(c.acts), c.occupancy, c.present)
        self.got, self.log, self.lags, self.via = np.zeros((T, F), bool), deque(maxlen=6), {}, None
        super().__init__(run, seed, care=care, upto=np.full(F, -1))
        for t in range(history):
            for m in self.feed(t):
                self.ingest(m, refresh=False)
        for f in range(F):
            self.refresh(f)

    def ingest(self, m, refresh=True):
        """One feed message in. A PHC's series are re-filtered once its day is in; a day it has
        already reported is ignored (Pub/Sub delivers at least once, in any order)."""
        with self.lock:
            kind, t, i = feeds.absorb(m, self.run, self.obs, self.care, skip=lambda k, t, i: k == "phc" and self.got[t, i])
            self.version += 1
            if kind != "phc" or self.got[t, i]:
                return kind, t, i
            self.got[t, i] = True
            before = int(self.upto[i])
            while self.upto[i] + 1 < self.days and self.got[self.upto[i] + 1, i]:
                self.upto[i] += 1
            if not refresh or self.upto[i] == before:
                return kind, t, i
            was = self.onset[max(before, 0), i] >= 0
            self.refresh(i)
            now = self.onset[self.upto[i], i] >= 0
            if isinstance(m.get("sent"), (int, float)):      # from report sent to estimate updated
                self.lags.setdefault(t, []).append(time.time() - m["sent"])
            if (now != was).any():      # the feed's log: reports that raised or cleared an alarm
                self.log.appendleft(dict(f=i, day=t, raised=np.flatnonzero(now & ~was).tolist(),
                                         cleared=np.flatnonzero(was & ~now).tolist()))
            return kind, t, i

    @property
    def start(self):
        t = self.clock
        hot = np.argwhere(self.onset[t] >= 0)
        f, j = map(int, hot[0]) if len(hot) else (0, 0)
        return dict(day=t, f=f, j=j)

    def status(self):
        with self.lock:
            return self._status()

    def _status(self):
        """day: the latest day any PHC has reported (a paper PHC may be ahead of the feed);
        through: the day every PHC has reported up to, which the feed sends on from."""
        t = self.clock
        lags = self.lags.get(t, [])
        return dict(day=t, through=int(self.upto.min()), last=self.days - 1, history=self.history, phcs=len(self.upto),
                    reported=int((self.upto >= t).sum()), complete=bool((self.upto >= t).all()), via=self.via,
                    seconds=round(float(np.median(lags)), 2) if lags else None, log=list(self.log))


def _kerala():
    run, coords, names = scenario.kerala()
    r = Replay(run, scenario.SEED, coords=coords, names=names, open_day=scenario.OPEN)
    r.real_sites = scenario.REAL_SITES
    # the script's numbers, measured: the first day any alarm is labelled a state failure, against GROUND TRUTH
    called = next(t for t in range(scenario.BREAK, r.days) if "STATE-PROCUREMENT" in r.labels(t).values())
    outs = [e["out"] for e in sim.stockout_events(run) if e["type"] == "STATE-PROCUREMENT"]
    starved = int(triage.starved(r.fill, called - triage.LOOK, called)[:, r.drugs].sum(0).max())
    r.about = dict(scenario.ABOUT, script=scenario.SCRIPT.format(
        start=scenario.BREAK, called=called, starved=starved, first=min(outs),
        after=f"all {len(outs)}" if min(outs) > called else f"{sum(o > called for o in outs)} of the {len(outs)}"))
    return r


NETS = {"demo": lambda: Replay(sim.simulate(seed=SEED), SEED), "kerala": _kerala, "live": lambda: Live(SEED + 1)}
nets, _building = {}, threading.Lock()
# What-if worlds kept, 36-62 MB each (whatif.py's bench). ponytail: the oldest goes first; an evicted
# world is rebuilt the same on its next request, losing only its shelf checks
WHATIFS, whatifs = 3, {}


def _whatif(key):
    try:
        params = whatif.parse(key)
    except ValueError as e:
        raise HTTPException(404, str(e))
    r = whatifs.get(key)
    if r is None:
        with _building:         # one build at a time: three at once took 17 s and +209 MB
            r = whatifs.get(key)
            if r is None:
                run, coords, names, about = whatif.build(params)
                r = Replay(run, about["whatif"]["seed"], coords=coords, names=names)
                r.id, r.about, r.real_sites = key, about, about["whatif"]["world"] == "kerala" and scenario.REAL_SITES
                while len(whatifs) >= WHATIFS:
                    whatifs.pop(next(iter(whatifs)))
                whatifs[key] = r
    return r


def _net(net: str = "demo") -> Replay:
    if net.startswith("whatif:"):
        return _whatif(net)
    if net not in NETS:
        raise HTTPException(404, f"net must be one of {', '.join(NETS)}")
    if net not in nets:
        with _building:
            if net not in nets:
                r = NETS[net]()
                r.id = net
                nets[net] = r
    return nets[net]


Net = Annotated[Replay, Depends(_net)]
replay = _net()
if os.environ.get("ANUMAAN_WARM"):
    for _name in [*NETS, *(m["key"] for m in whatif.MENU)]:
        _net(_name)
app = FastAPI(title="Anumaan")


def locked(endpoint):
    """Run an endpoint under its network's lock: a report landing mid-request must not change
    the view under it (alarm labels are worked out first, then read cell by cell)."""
    @wraps(endpoint)
    def run(r, *args, **kwargs):
        with r.lock:
            return endpoint(r, *args, **kwargs)
    return run


def _day(r, t):
    if not 0 <= t <= r.clock:
        raise HTTPException(404, f"day must be in 0..{r.clock}")
    return t


def _cell_ok(r, f, j):
    if not (0 <= f < len(r.run.facilities) and 0 <= j < len(r.drugs)):
        raise HTTPException(404, "no such facility/medicine")


def _num(x):
    """JSON-safe float: NaN (e.g. no forecast band yet) becomes null."""
    return None if x is None or np.isnan(x) else round(float(x), 1)


SITES = {False: "SYNTHETIC points: not real facility locations.",
         True: "Real public PHC/FHC locations from OpenStreetMap (map data (c) OpenStreetMap contributors, ODbL 1.0); "
               "every record at them is synthetic, and the facilities' names are not shown. The statuses shown are "
               "simulated and say nothing about these facilities."}


@app.get("/api/meta")
@locked
def meta(r: Net):
    run, crg = r.run, G.load()
    return dict(net=r.id, days=r.days, clock=r.clock, synthetic=True, grammar=crg["version"], grammar_note=crg["source_note"],
                start=r.start, horizon=HORIZON, low=FL.LOW, names=r.names,
                real_roads=PL._road_arcs(r.coords) is not None, live=r.status() if r.live else None,
                scenario=getattr(r, "about", None), real_sites=r.real_sites, sites_note=SITES[r.real_sites],
                facilities=[dict(id=i, wh=w, st=s, lat=round(float(a), 5), lon=round(float(b), 5))
                            for i, w, s, (a, b) in zip(run.facilities, run.wh, run.st, r.coords)],
                drugs=[dict(name=run.ix["drugs"][d], unit=crg["drugs"][run.ix["drugs"][d]]["unit"]) for d in r.drugs],
                cadres=CARE.CADRES, levels=triage.LEVELS, actions=triage.ACTION, languages=voice.LANGUAGE_INFO)


@app.get("/api/day/{t}")
@locked
def day(r: Net, t: int, truth: bool = False):
    labels = r.labels(_day(r, t))
    cells = [r.cell(t, f, j, labels, truth) for f in range(len(r.run.facilities)) for j in range(len(r.drugs))]
    summary = {k: sum(c["regime"] == k for c in cells) for k in FL.REGIMES}
    summary.update(phantom=sum(c["phantom"] for c in cells),
                   **{lv: sum(c.get("level") == lv for c in cells) for lv in triage.LEVELS})
    return dict(day=t, cells=cells, summary=summary)


@app.get("/api/series/{f}/{j}")
@locked
def series(r: Net, f: int, j: int, t: int, truth: bool = False):
    _cell_ok(r, f, j)
    run, obs = r.run, r.obs
    d, sl = r.drugs[j], slice(0, min(_day(r, t), int(r.upto[f])) + 1)
    out = {k: obs[k][sl, f, d].round(2).tolist() for k in ("N", *G.CATS)}
    out.update(book=run.book[sl, f, d].round().tolist(), use=r.use[sl, f, j].round(1).tolist(),
               receipts=run.receipts[sl, f, d].round().tolist(), gap=r.skip[f][sl].tolist(),
               p_scarce=r.post[sl, f, j, 1].round(3).tolist(), p_out=r.post[sl, f, j, 2].round(3).tolist())
    if truth:
        out["true"] = run.true_stock[sl, f, d].round().tolist()
    return out


@app.get("/api/forecast/{f}/{j}")
@locked
def forecast(r: Net, f: int, j: int, t: int):
    """Next HORIZON days of units: from diagnoses through the CRG, and the fair
    consumption baseline (dispensing with out-days filled from the in-stock rate)."""
    _cell_ok(r, f, j)
    obs, d, t = r.obs, r.drugs[j], min(_day(r, t), int(r.upto[f]))
    mean, lo, hi = FC.forecast_series(obs["exp_units"][:t + 1, f, d], HORIZON)
    units = obs["units"][:, f, d]
    adj = FC.adjust_consumption(units, (obs["na"][:, f, d] > 0) | (obs["sub"][:, f, d] > 0) | (units == 0), t)
    cons = FC.forecast_series(adj[:t + 1], HORIZON)[0]
    return dict(days=list(range(t + 1, t + 1 + HORIZON)), mean=[_num(x) for x in mean],
                lo=[_num(x) for x in lo], hi=[_num(x) for x in hi],
                total=round(float(mean.sum())), consumption_total=round(float(cons.sum())))


@app.get("/api/facility/{f}")
@locked
def facility(r: Net, f: int, t: int, truth: bool = False):
    """Beds from the ADT feed and staff from attendance + acts, for one PHC on day t."""
    _cell_ok(r, f, 0)
    row = r.board(_day(r, t), truth)["rows"][f]
    beds = {k: row[k] for k in ("capacity", "occupied", "free", "pressure", "early_share_7d", "refer", "true_occupied") if k in row}
    return dict(beds=beds, staff=row["staff"])


@app.get("/api/care")
@locked
def care(r: Net, t: int, truth: bool = False):
    """The beds and staff board for day t: every PHC, where a full one can send a patient,
    and the attendance marks to verify."""
    return r.board(_day(r, t), truth)


# "Where it broke", in the district screen's words (app.js's evidence() says the same at length)
BROKE = {"LOCAL": "at the PHC itself", "WAREHOUSE": "at the district warehouse", "STATE-PROCUREMENT": "in the state's supply",
         "NATIONAL": "in more than one state's supply", "DEMAND-SURGE": "not a break: demand is up"}


def _area(r, t, fs, cells, board, p):
    """One district's day (or the network's), from /api/day's cells, /api/care's board and
    /api/plan: the same numbers as those views. A PHC that has not reported day t counts with
    its last report, as they show it."""
    run, conds, ids, mine = r.run, r.run.ix["conds"], {r.run.facilities[f] for f in fs}, set(fs)
    at = np.minimum(t, r.upto)[fs]
    today = np.array([run.dx[a, f] for a, f in zip(at, fs)]).sum(0)                       # [C]
    usual = np.array([run.dx[max(a - USUAL, 0):a, f].mean(0) if a else np.full(len(conds), np.nan)
                      for a, f in zip(at, fs)]).sum(0)
    up = sorted((k for k in range(len(conds)) if today[k] > usual[k]), key=lambda k: usual[k] - today[k])[:2]
    n = lambda x: None if np.isnan(x) else round(float(x), 1)
    risk = []
    for j, d in enumerate(r.drugs):
        hot = [c for c in cells if c["j"] == j and c["alarm"] and c["f"] in mine]
        if hot:
            where = Counter(c["level"] for c in hot).most_common()
            risk.append(dict(j=j, medicine=run.ix["drugs"][d], alarm=len(hot), hidden=sum(c["phantom"] for c in hot),
                             where=[dict(level=lv, phcs=k, words=BROKE[lv]) for lv, k in where]))
    rows = [board["rows"][f] for f in fs]
    staff = [x for row in rows for x in row["staff"]]
    moves = [x for x in p["transfers"] if x["to_fac"] in ids]      # a transfer counts in its recipient's district
    return dict(
        phcs=len(fs), behind=int((at < t).sum()),
        footfall=dict(today=int(today.sum()), usual=n(usual.sum()),
                      change=n(100 * (today.sum() / usual.sum() - 1)) if usual.sum() > 0 else None,
                      up=[dict(condition=conds[k], today=int(today[k]), usual=n(usual[k])) for k in up]),
        medicines=sorted(risk, key=lambda x: (-x["hidden"], -x["alarm"], x["j"])),
        beds=dict(capacity=sum(x["capacity"] for x in rows), free=sum(x["free"] for x in rows),
                  full=[dict(f=x["f"], refer=x["refer"]) for x in rows if x["pressure"]]),
        staff=dict(verify=sum(x["verify"] for x in staff),
                   verify_by_cadre={k: sum(x["verify"] for x in staff if x["cadre"] == k) for k in CARE.CADRES},
                   vacant=sum(max(x["sanctioned"] - x["in_position"], 0) for x in staff)),
        actions=dict(transfers=len(moves), to_approve=sum(not x["approved"] for x in moves),
                     courses=sum(x["courses"] for x in moves),
                     escalations=[dict(f=run.facilities.index(e["fac"]), drug=e["drug"], level=e["level"])
                                  for e in p["escalations"] if e["fac"] in ids]))


@app.get("/api/district")
@locked
def district(r: Net, t: int, wh: str | None = None):
    """The district officer's first screen for day t: diagnoses recorded against usual, medicines
    at risk and where they broke, beds, staff, and what to approve or escalate. Without wh, the
    whole network, with a line for each district and the one with the most to do."""
    if wh is not None and wh not in r.whs:
        raise HTTPException(404, f"wh must be one of {', '.join(r.whs)}")
    run, t = r.run, _day(r, t)
    labels, board, p = r.labels(t), r.board(t), plan(r, t)
    cells = [r.cell(t, f, j, labels, False) for f in range(len(run.facilities)) for j in range(len(r.drugs))]
    area = lambda w: _area(r, t, [f for f in range(len(run.facilities)) if w in (None, run.wh[f])], cells, board, p)
    place = lambda w: {k: r.place(run.facilities[run.wh.index(w)])[k] for k in ("state", "warehouse")}
    out = dict(day=t, wh=wh, place=place(wh) if wh else None, usual_days=USUAL, footfall_is=FOOTFALL, **area(wh))
    if wh is None:
        out["districts"] = []
        for w in r.whs:
            a = area(w)
            act, alarms = a["actions"], a["medicines"]
            out["districts"].append(dict(
                wh=w, place=place(w), phcs=a["phcs"], alarm=sum(m["alarm"] for m in alarms),
                hidden=sum(m["hidden"] for m in alarms), full=len(a["beds"]["full"]), verify=a["staff"]["verify"],
                to_approve=act["to_approve"], escalations=len(act["escalations"]),
                todo=act["to_approve"] + len(act["escalations"]) + a["staff"]["verify"]))
        out["busiest"] = max(out["districts"], key=lambda x: x["todo"])["wh"]
    return out


@app.get("/api/plan")
@locked
def plan(r: Net, t: int):
    """Redistribution for day t: same-state transfers in treatment courses (OR-Tools
    min-cost flow), escalations where moving stock cannot help, DVDMS-style orders."""
    run, obs = r.run, r.obs
    labels = PL.labels_at(_day(r, t), run, r.post_d, obs, r.cover_d)
    p = PL.plan(t, run, r.post_d, labels, r.coords, horizon=HORIZON, obs=obs, cover=r.cover_d)
    for x in p["transfers"]:
        x["approved"] = (r.id, t, x["from_fac"], x["to_fac"], x["drug"]) in ledger
    return p


class Approve(BaseModel):
    t: int
    from_fac: str
    to_fac: str
    drug: str
    net: str | None = None      # the network whose plan the officer saw; without it, ?net=


@app.post("/api/approve")
def approve(r: Net, a: Approve):
    """The district officer's one click: a planned transfer becomes an issue order in the ledger.
    409 when it is not in day t's plan for that network: a page left over from another network,
    day or feed."""
    r = _net(a.net) if a.net else r
    with r.lock:
        if 0 <= a.t <= r.clock:
            p = plan(r, a.t)
            for x, order in zip(p["transfers"], p["orders"]):
                key = (r.id, a.t, x["from_fac"], x["to_fac"], x["drug"])
                if key == (r.id, a.t, a.from_fac, a.to_fac, a.drug):
                    return ledger.setdefault(key, dict(order, day=a.t, courses=x["courses"],
                                                       approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))
    raise HTTPException(409, f"that transfer is not in day {a.t}'s plan for this network: reload the plan")


@app.get("/api/ledger")
def orders(r: Net):
    return [o for key, o in ledger.items() if key[0] == r.id]


def _facts(r, t, f, j):
    """What Anumaan knows about one PHC x medicine on day t, for the officer's brief."""
    run, obs, d = r.run, r.obs, r.drugs[j]
    c, last, name = r.cell(t, f, j, r.labels(t), False), slice(max(0, t - 13), t + 1), run.ix["drugs"][d]
    where = lambda fid: "{phc}, {warehouse}".format(**r.place(fid))
    moves = [dict(courses=x["courses"], from_phc=where(x["from_fac"]), to_phc=where(x["to_fac"]), minutes=x["minutes"])
             for x in plan(r, t)["transfers"] if x["drug"] == name and run.facilities[f] in (x["from_fac"], x["to_fac"])]
    return dict(medicine=name.replace("_", " "), **r.place(run.facilities[f]), day=t,
                register_units=c["book"], register_days_of_use=c["cover"],
                inferred_shelf={"OK": "stocked", "SCARCE": "running short", "OUT": "empty"}[c["regime"]],
                inferred_confidence=round(max(c["p"]), 2), days_of_use_left_deliveries_minus_dispensing=c["shadow"],
                warning_line_days=FL.LOW, pharmacist_check=c["confirmed"], alarm=c["alarm"],
                alarm_since_day=c.get("onset"), where_it_broke=c.get("level"), suggested_action=c.get("action"),
                warehouse_indent_fill_rate=c.get("fill"), statewide_diagnosis_lift=c.get("lift"),
                district_diagnosis_lift=c.get("district_lift"),
                last_14_days={k: int(round(obs[o][last, f, d].sum())) for k, o in (
                    ("courses_called_for", "N"), ("given_in_full", "full"), ("cut_short", "ration"),
                    ("switched_to_substitute", "sub"), ("marked_not_available", "na"))},
                planned_transfers=moves)


def _brief_key(r, f, j, t, language):
    """(facts, key): a brief's evidence, and its cache key, so the same evidence never pays twice."""
    _cell_ok(r, f, j)
    if language not in voice.LANGUAGES:
        raise HTTPException(422, f"language must be one of {', '.join(voice.LANGUAGES)}")
    with r.lock:
        facts = _facts(r, _day(r, t), f, j)
    return facts, (json.dumps(facts, sort_keys=True), language)


@app.get("/api/brief/{f}/{j}")
async def brief(r: Net, f: int, j: int, t: int, language: str = "en"):
    """Gemini turns the evidence for one PHC x medicine into a short brief for the district officer,
    in any of voice.LANGUAGES. A brief that fails voice.check (a number not in the evidence, or too
    little of the language's script) comes back with its problems: show the English one instead.
    Asked again, a brief that failed is written afresh (another call); one that passed is served again."""
    facts, key = _brief_key(r, f, j, t, language)
    if key not in briefs or "problems" in briefs[key]:
        _spend()
        try:
            briefs[key] = await run_in_threadpool(voice.write_brief, facts, language)
        except voice.VoiceUnavailable as e:
            raise HTTPException(503, str(e))
    return briefs[key]


@app.get("/api/brief/{f}/{j}/audio")
async def brief_audio(r: Net, f: int, j: int, t: int, language: str = "en"):
    """The brief /api/brief wrote for this evidence, read aloud by Gemini-TTS as WAV: paid once a
    brief, against the same daily cap. 409 until that brief is written, or if it failed its check."""
    _, key = _brief_key(r, f, j, t, language)
    if not voice.LANGUAGE_INFO[language]["voice"]:
        raise HTTPException(422, f"no Google voice reads {voice.LANGUAGES[language]} yet")
    b, clip = briefs.get(key), clips.get(key)
    if b is None or "problems" in b:
        raise HTTPException(409, "write the brief first" if b is None else "this brief failed its check: play the English one")
    if clip is None:
        _spend()
        try:
            clip = clips[key] = await run_in_threadpool(voice.speak, f"{b['summary']} {b['next_step']}", language)
        except ValueError as e:     # refused before any Gemini call: give the slot back
            gemini_used[date.today()] -= 1
            raise HTTPException(422, str(e))
        except voice.VoiceUnavailable as e:
            raise HTTPException(503, f"Gemini-TTS could not read the brief aloud: {e}")
        while len(clips) > CLIPS:
            clips.pop(next(iter(clips)))
    return Response(clip[0], media_type=clip[1])


@app.get("/api/national")
@locked
def national(r: Net, t: int):
    """What the national project sees: each state's export through the clean-room gate."""
    nat = FED.National()
    exports = [n.export(_day(r, t)) for n in r.nodes]
    for ex in exports:
        nat.ingest(ex)                  # raises if an export carries anything raw
    return dict(exports=exports, view=nat.view(), priors=nat.priors(),
                raw_rows={n.state: len(n.slips) + len(n.na) + int((n.dx > 0).sum()) for n in r.nodes},
                export_bytes={ex["state"]: len(json.dumps(ex)) for ex in exports})


class Confirm(BaseModel):
    f: int
    j: int
    t: int
    answer: Literal["empty", "available"]


def _apply(r, f, j, t, answer):
    with r.lock:
        r.confirm.setdefault((f, j), {})[t] = answer
        r.refilter(f, j)
    return r.post[t, f, j].round(3).tolist()


@app.post("/api/confirm")
@locked
def confirm(r: Net, c: Confirm):
    """A pharmacist's shelf check becomes evidence for that day; only this series is re-filtered."""
    _cell_ok(r, c.f, c.j)
    return dict(ok=True, p=_apply(r, c.f, c.j, _day(r, c.t), c.answer))


@app.post("/api/voice")
async def voice_confirm(r: Net, request: Request, f: int, j: int, t: int, language: str | None = None):
    """A spoken shelf check: Gemini transcribes and labels it; a clear yes/no updates the
    estimate exactly like the buttons. 503 when Gemini is not configured."""
    _cell_ok(r, f, j)
    _day(r, t)
    _spend()
    audio = await request.body()
    mime = request.headers.get("content-type", "audio/webm")
    try:   # label_voice blocks for up to 30 s; keep it off the event loop
        heard = await run_in_threadpool(voice.label_voice, audio, mime,
                                        r.run.ix["drugs"][r.drugs[j]], language)
    except ValueError as e:     # rejected before any Gemini call: give the slot back
        gemini_used[date.today()] -= 1
        raise HTTPException(422, str(e))
    except voice.VoiceUnavailable as e:
        raise HTTPException(503, str(e))
    if heard["answer"] in ("empty", "available"):
        heard["p"] = _apply(r, f, j, t, heard["answer"])
    return heard


# ---------- the live feed ----------

@app.get("/api/live")
def live_status():
    return _net("live").status()


@app.post("/api/live/step")
def live_step(days: int = 1):
    """Send the next `days` days' reports (1 to 14), one message per PHC and per warehouse a day:
    through Pub/Sub when a topic is configured (they come back by push to /api/ingest), else applied
    in-process. The feed goes on from the day every PHC has reported (status "through"), so a paper
    PHC that reported ahead never holds it up: the feed's copy of a day it already sent is ignored.
    Re-sends a day still in flight, which is harmless: a report already in is ignored."""
    if not 1 <= days <= 14:
        raise HTTPException(422, "days must be 1 to 14")
    r = _net("live")
    t0 = int(r.upto.min()) + 1
    if t0 >= r.days:
        raise HTTPException(409, "The feed has reached its last day. Restart it to run again.")
    sent, last = 0, min(t0 + days, r.days) - 1
    for t in range(t0, last + 1):
        msgs, now = r.feed(t), time.time()
        for m in msgs:
            m["sent"] = now
        if TOPIC:
            try:
                feeds.publish(TOPIC, msgs)
            except Exception as e:      # any failure to reach Pub/Sub: say so, the day can be sent again
                logging.exception("publish to %s failed", TOPIC)
                raise HTTPException(502, f"Could not publish to Pub/Sub ({type(e).__name__}).")
            seen["publish"] = time.time()
        else:
            for m in msgs:
                r.ingest(m)
        sent += len(msgs)
    r.via = "Pub/Sub" if TOPIC else "direct"
    return dict(day=last, days=last - t0 + 1, messages=sent, via=r.via)


@app.post("/api/live/reset")
def live_reset():
    """Start the feed again from its history. ponytail: one feed shared by every visitor."""
    with _building:
        nets.pop("live", None)
    return _net("live").status()


@app.post("/api/ingest")
async def ingest(request: Request, token: str = ""):
    """One feed message (feeds.stream's format), bare or wrapped by a Pub/Sub push subscription.
    A message that can never be read is answered 200 so Pub/Sub stops redelivering it."""
    if not INGEST_TOKEN or not hmac.compare_digest(token.encode(), INGEST_TOKEN.encode()):
        raise HTTPException(403, "ingest needs the feed's token")
    try:
        body = json.loads(await request.body())
        pushed = "message" in body
        m = json.loads(base64.b64decode(body["message"]["data"])) if pushed else body
        r = await run_in_threadpool(_net, "live")
        kind, t, _ = await run_in_threadpool(r.ingest, m)
    except (ValueError, KeyError, TypeError) as e:
        return dict(ok=False, error=str(e))
    if pushed:
        seen["push"] = time.time()
    return dict(ok=True, kind=kind, day=(feeds.START + timedelta(t)).isoformat())


# ---------- a paper PHC: photos of its registers, read by Gemini, checked by a person, into the live feed ----------

class Photo(BaseModel):
    mime: str
    data: str       # base64: FastAPI's file uploads need python-multipart, which the image does not carry


class Pages(BaseModel):
    phc: str
    date: str
    images: list[Photo]


B64_MAX = 4 * -(-intake.MAX_BYTES // 3)     # the base64 length of the largest photo intake takes


class Read(BaseModel):
    phc: str
    date: str
    read: dict


Count = Annotated[float, Field(ge=0, le=100_000)]


class PaperDay(BaseModel):
    """All a paper day can carry into the shared live network: no register balance (feeds.absorb
    keeps the last one), no negative or outsized numbers, nothing else."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["phc"]
    date: str
    phc: str
    diagnoses: dict[str, Annotated[int, Field(ge=0, le=10_000)]]
    slips: list[tuple[str, str, Annotated[int, Field(ge=0, le=365)], Count]] = []
    not_available: list[tuple[str, str]] = []
    received: dict[str, Count] = {}


def _paper_day(r, phc, when):
    """(day, PHC index) of a paper PHC's report, which must be its next day. Never checked against
    the feed's day: a paper PHC may report ahead of the feed."""
    if phc not in r.run.facilities:
        raise HTTPException(404, f"no PHC {phc!r} in the live network")
    try:
        t = (date.fromisoformat(when) - feeds.START).days
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    f = r.run.facilities.index(phc)
    if not 0 <= t < r.days:
        raise HTTPException(422, f"{when} is outside the live network's {r.days} days")
    if r.got[t, f]:
        raise HTTPException(409, f"{phc} has already reported {when}")
    if t != r.upto[f] + 1:
        raise HTTPException(409, f"{phc} reports {feeds.START + timedelta(int(r.upto[f]) + 1)} next")
    return t, f


def _message(read, phc, when):
    try:
        message, review = intake.to_message(read, phc, when)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return dict(message=message, review=review)


@app.post("/api/intake/read")
async def intake_read(p: Pages):
    """Gemini reads photos of a paper PHC's OPD and dispensing registers for one day (one call,
    against the daily cap) into that day's feed message, and lists the lines a person should
    check. Nothing is applied here: /api/intake/confirm does that once a person has checked it.
    Every check runs before the call is spent, and the count and sizes before anything is decoded."""
    if not 1 <= len(p.images) <= intake.MAX_IMAGES or any(len(x.data) > B64_MAX for x in p.images):
        raise HTTPException(422, f"send 1 to {intake.MAX_IMAGES} photos of the register pages, "
                                 f"each at most {intake.MAX_BYTES >> 20} MB")
    r = await run_in_threadpool(_net, "live")
    with r.lock:
        _paper_day(r, p.phc, p.date)
    try:
        images = [(base64.b64decode(x.data, validate=True), x.mime) for x in p.images]
    except ValueError:
        raise HTTPException(422, "each photo's data must be base64")
    try:
        images = intake.check(images)
    except ValueError as e:     # a bad photo: refused before the call is spent
        raise HTTPException(422, str(e))
    _spend()
    t0 = time.time()
    try:
        read = await run_in_threadpool(intake.read, images)
    except ValueError as e:     # a bad photo is refused before any Gemini call: give the slot back
        gemini_used[date.today()] -= 1
        raise HTTPException(422, str(e))
    except voice.VoiceUnavailable as e:
        raise HTTPException(503, str(e))
    return dict(read=read, seconds=round(time.time() - t0, 1), **_message(read, p.phc, p.date))


@app.post("/api/intake/check")
def intake_check(c: Read):
    """A read a person has corrected -> the day's message and the lines still to check. No Gemini call.
    It answers 404 or 409 as confirm would, so a day that can no longer be applied says so at once."""
    r = _net("live")
    with r.lock:
        _paper_day(r, c.phc, c.date)
    return _message(c.read, c.phc, c.date)


@app.post("/api/intake/confirm")
def intake_confirm(p: PaperDay):
    """A paper day a person has checked (the message from /api/intake/read or /check, plus any
    "received" units) into the live network, once: a second confirm of the same day is a 409."""
    r = _net("live")
    with r.lock:
        t, _ = _paper_day(r, p.phc, p.date)
        try:
            r.ingest(p.model_dump())
        except ValueError as e:
            raise HTTPException(422, str(e))
        return dict(ok=True, phc=p.phc, day=t, status=r._status())


# ---------- what-if worlds ----------

@app.get("/api/whatif")
def whatif_choices():
    """What the what-if form offers: each world's districts, the medicines, lifts and days, and the menu."""
    return whatif.choices()


@app.post("/api/whatif")
def whatif_open(params: dict):
    """{world, event, and the event's fields} -> {net, about}: the world is built (one at a time)
    or found, and every other endpoint answers for it with ?net=<net>."""
    try:
        key = whatif.validate(params)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return dict(net=key, about=_whatif(key).about)


# ---------- which Google services are doing what, honestly ----------

def _at(ts):
    """A moment this process saw, in IST; None if it has not."""
    return None if ts is None else datetime.fromtimestamp(ts, IST).isoformat(timespec="seconds")


def _timesfm():
    """The TimesFM row's words, from the pre-registered re-test's own results."""
    res = json.loads(Path(FC.__file__).with_name("timesfm_retest.json").read_text(encoding="utf-8"))["results"]
    mean = {s: {k.split(":")[0]: v["mean"] for k, v in res["held_out"][s].items() if isinstance(v, dict)}
            for s in ("5-9", "10-14")}
    w = float(res["seeds_0_4"]["winner"].rsplit(" ", 1)[1])
    ran = datetime.fromisoformat(res["queries"][0]["job_created"].replace("Z", "+00:00")).astimezone(IST)
    return (f"Pre-registered re-test, {len(res['queries'])} BigQuery AI.FORECAST queries: {w:g} x the ETS + {1 - w:g} x "
            f"TimesFM had a lower 14-day demand error (WAPE) than the ETS alone on held-out seeds 5-9 "
            f"({mean['5-9']['winner']:.4f} vs {mean['5-9']['ETS alone']:.4f}) and 10-14 ({mean['10-14']['winner']:.4f} "
            f"vs {mean['10-14']['ETS alone']:.4f}); TimesFM alone trails ({mean['5-9']['information_only']:.4f}, "
            f"{mean['10-14']['information_only']:.4f}), and a plain mean of past demand, not a candidate, does about as "
            f"well or better ({mean['5-9']['reference_history_mean']:.4f}, {mean['10-14']['reference_history_mean']:.4f}). "
            f"SYNTHETIC data; the app does not call it."), ran.isoformat(timespec="seconds")


def _roads():
    """The Routes row's words and date: the road times road_minutes.json holds, by the day they were fetched."""
    maps = json.loads(PL.ROAD_MINUTES.read_text())["maps"] if PL.ROAD_MINUTES.exists() else {}
    on = {}
    for name, m in maps.items():
        on.setdefault(m["fetched"], []).append((name, m["elements"]))
    return "; ".join(f"{sum(n for _, n in v):,} pairs fetched {d} (maps {', '.join(k for k, _ in v)})"
                     for d, v in sorted(on.items())), max(on, default=None)


@app.get("/api/google")
def google():
    """Each Google service Anumaan uses, and what this instance can honestly say of it: "live" only
    with `at`, a moment this process saw it answer; "configured" is set up but not seen answering;
    offline and cached services give the date of their evidence (`as_of`). Nothing is probed."""
    voice.load_env()
    vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1") and all(
        os.environ.get(k) for k in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"))
    tts = os.environ.get("ANUMAAN_TTS_MODEL", voice.TTS_MODEL)
    said = {k: v for k, v in voice.answered.items() if k != tts}
    model = max(said, key=said.get, default=None)
    seen_by = lambda at, ok: dict(status="live" if at else "configured" if ok else "not configured", at=_at(at))
    push, svc, crg = seen.get("push"), os.environ.get("K_SERVICE"), G.load()
    roads, fetched = _roads()
    timesfm, queried = _timesfm()
    row = lambda **kw: dict(dict(at=None, as_of=None), **kw)
    return dict(now=_at(time.time()), started=_at(STARTED), services=[
        row(service="Gemini on Vertex AI", **seen_by(said.get(model), vertex),
            used_for="the district officer's brief, the spoken shelf check and reading paper registers",
            detail=f"{gemini_used.get(date.today(), 0)} of {GEMINI_CAP} calls today on this instance"
                   + (f"; last answer from {model}" if model else ""), source="calls made by this process"),
        row(service="Text-to-Speech (Gemini-TTS on Vertex AI)", **seen_by(voice.answered.get(tts), vertex),
            used_for="reading the brief aloud", source="calls made by this process",
            detail=f"{tts}: a voice for {sum(bool(v['voice']) for v in voice.LANGUAGE_INFO.values())} of the "
                   f"{len(voice.LANGUAGE_INFO)} brief languages"),
        row(service="Pub/Sub", status="live" if push else "configured" if TOPIC else "off", at=_at(push),
            used_for="the live feed: each PHC's day as a message, pushed back to /api/ingest",
            detail=("a topic is set" if TOPIC else "no topic set: the live feed is applied in-process")
                   + (f"; last published {_at(seen['publish'])}" if "publish" in seen else ""),
            source="pushes received and days published by this process"),
        row(service="Cloud Run", status="live" if svc else "off", at=_at(STARTED) if svc else None,
            used_for="serves the app and the API", source="K_SERVICE and K_REVISION, set by Cloud Run",
            detail=f"service {svc}, revision {os.environ.get('K_REVISION')}, this instance started {_at(STARTED)}"
                   if svc else "not running on Cloud Run (K_SERVICE is unset)"),
        row(service="BigQuery clean room", status="offline", as_of="2026-09-29T23:44:35+05:30",
            used_for="each state shares only warehouse x medicine x day rows with 5 or more PHCs behind them",
            detail="proof last run 29 Sep 2026 23:44 IST, 8,400 rows. The app shows the same gate computed in Python: "
                   "its runtime identity may call only Gemini.",
            source="BigQuery table metadata of anumaan_national.warehouse_day (bq show), written "
                   "by python -m anumaan.cleanroom proof"),
        row(service="Google Maps Routes API", status="cached", used_for="drive times for the transfers",
            detail=f"{roads}; read from a file, never called at runtime", source="anumaan/road_minutes.json"),
        row(service="Gemini grammar compiler", status="offline",
            used_for="turned treatment guidelines into the care-to-resource grammar the filter reads",
            detail=f"{crg['version']}, from {crg['source_note'].count('.pdf')} guideline PDFs",
            source="grammar/crg/compiled.json"),
        row(service="TimesFM on BigQuery (AI.FORECAST)", status="offline",
            used_for="a 14-day demand forecast, tested against the app's own", detail=timesfm,
            source="anumaan/timesfm_retest.json")])


@app.get("/api/eval")
def eval_():
    """Held-out scores, always of the demo network: a scripted or live network is not a test set."""
    m = replay.metrics
    out = {k: v for k, v in m.items() if not k.startswith("triage_") and k != "types"}
    out["types"] = dict(m["types"])
    out["triage_majority"] = m["triage_majority"]
    for k in ("triage_7d", "triage_21d"):
        out[k] = {x: y for x, y in m[k].items() if x != "confusion"}
        out[k]["confusion"] = [dict(truth=a, pred=b, n=n) for (a, b), n in m[k]["confusion"].items()]
    return out


@app.get("/api/real")
def real():
    """The premise checked on REAL dispensing records (England's; India publishes none): see anumaan/realcheck.py."""
    return realcheck.summary()


@app.get("/api/real/india")
def real_india():
    """The premise checked on India's HMIS district-month records, its rules fixed before any
    result: see anumaan/realcheck_india.py."""
    return realcheck_india.summary()


@app.get("/api/truth/episodes")
def episodes(r: Net):
    run = r.run
    return [dict(e, drug=run.ix["drugs"][e["drug"]]) for e in run.episodes]


SAMPLES = Path(__file__).parent.parent / "tools" / "samples"     # SYNTHETIC register pages for the intake's "use a sample"
if SAMPLES.is_dir():
    app.mount("/samples", StaticFiles(directory=SAMPLES), name="samples")
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
