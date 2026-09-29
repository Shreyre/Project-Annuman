"""Anumaan demo service: replays a SYNTHETIC PHC network through the filter and
serves the Counter Truth, beds and staff, forecast, redistribution and national
views. One container: API + static UI.

    uvicorn app.main:app --reload
"""
import json
import os
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from anumaan import care as CARE, crg as G, federation as FED, filter as FL, forecast as FC
from anumaan import planner as PL, sim, triage, voice
from anumaan.evaluate import evaluate

THR = 0.7
HORIZON = 14
# The public demo link lets anyone trigger a paid Gemini call, so cap voice per day.
# ponytail: per-process counter; with several Cloud Run instances each gets its own cap
VOICE_CAP = int(os.environ.get("ANUMAAN_VOICE_DAILY_CAP", 200))
voice_used = {}


class Replay:
    """Everything the UI can ask for, precomputed. The filter is causal, so the
    view at day t only ever reflects data up to day t."""

    def __init__(self, seed):
        self.seed = seed
        self.run = run = sim.simulate(seed=seed)
        self.obs = obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
        self.drugs = P = list(run.ix["primaries"])
        T, F, J = run.book.shape[0], len(run.facilities), len(P)
        given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
        self.skip = [FL.entry_gaps(given[:, f], obs["N"][:, f, P].sum(1)) for f in range(F)]
        self.post, self.cover = np.zeros((T, F, J, 3)), np.zeros((T, F, J))
        self.use = np.stack([FL.trailing_mean(obs["exp_units"][:, f, d]) for f in range(F) for d in P], 1).reshape(T, F, J)
        self.onset = np.full((T, F, J), -1)
        self.segs, self.confirm = {}, {}
        for f in range(F):
            for j in range(J):
                self.refilter(f, j)
        # views onto self.post / self.cover keyed by drug index, as the planner expects; refilter writes in place
        self.post_d = {(f, d): self.post[:, f, j] for f in range(F) for j, d in enumerate(P)}
        self.cover_d = {(f, d): self.cover[:, f, j] for f in range(F) for j, d in enumerate(P)}
        self.whs = sorted(set(run.wh))
        self.fill = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted)
        self.lift = triage.surge_lift(obs["N"], run.st)
        self.coords = PL.synthetic_coords(run, seed)
        self.out_mask = (obs["na"] > 0) | (obs["sub"] > 0) | (obs["units"] == 0)
        self.care = CARE.simulate(run, seed)
        self.beds, self.staff = CARE.beds(self.care), CARE.staff(self.care)
        self.nodes = [FED.StateNode(run, s) for s in sorted(set(run.st))]
        self.raw_rows = {n.state: len(n.slips) + len(n.na) + int((n.dx > 0).sum()) for n in self.nodes}
        self.metrics = evaluate(run)

    def refilter(self, f, j):
        run, obs, d = self.run, self.obs, self.drugs[j]
        cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
        N, rec, book, exp = obs["N"][:, f, d], run.receipts[:, f, d], run.book[:, f, d], obs["exp_units"][:, f, d]
        rho, tau = FL.learn(N, cats, book, exp, rec, self.skip[f])
        checks = self.confirm.get((f, j))
        p = FL.filter_series(N, cats, book, exp, rec, self.skip[f], tau=tau, confirm=checks, rho=rho)
        self.post[:, f, j] = p
        self.cover[:, f, j] = FL.shadow_cover(book, rec, obs["units"][:, f, d], FL.trailing_mean(rho * exp), p,
                                              self.skip[f], checks)
        self.segs[(f, j)] = segs = FL.alarms(p, THR, self.cover[:, f, j])
        self.onset[:, f, j] = -1
        for s, e in segs:
            self.onset[s + 1:e, f, j] = s     # an alarm needs 2 days: live from s+1

    def phantom(self, t, f, j):
        return self.onset[t, f, j] >= 0 and self.post[t, f, j, 2] >= THR and \
            self.run.book[t, f, self.drugs[j]] >= 7 * max(self.use[t, f, j], 1e-9)

    def labels(self, t):
        known = [(f, j, s) for (f, j), segs in self.segs.items() for s, _ in segs if s + 1 <= t]
        onsets = [(f, self.drugs[j], s) for f, j, s in known]      # triage indexes drugs, not columns
        return dict(zip(known, triage.classify(onsets, self.run.wh, self.run.st, self.fill, self.lift, t)))

    def cell(self, t, f, j, labels, truth):
        run, d = self.run, self.drugs[j]
        p = self.post[t, f, j]
        book = run.book[t, f, d]
        c = dict(f=f, j=j, book=round(book), cover=round(book / max(self.use[t, f, j], 1e-9), 1),
                 shadow=round(float(self.cover[t, f, j]), 1),
                 p=[round(float(x), 3) for x in p], regime=FL.REGIMES[int(p.argmax())],
                 alarm=bool(self.onset[t, f, j] >= 0), phantom=bool(self.phantom(t, f, j)),
                 confirmed=(self.confirm.get((f, j)) or {}).get(t))
        if c["alarm"]:
            s = int(self.onset[t, f, j])
            fill = self.fill[t, self.whs.index(run.wh[f]), d]
            c.update(onset=s, level=labels[(f, j, s)], action=triage.ACTION[labels[(f, j, s)]],
                     by_stock=bool(p[1] + p[2] < THR),   # live only because the shadow stock is low
                     fill=None if np.isnan(fill) else round(float(fill), 2), lift=round(float(self.lift[t, f, d]), 2))
        if truth:
            c["true"] = round(run.true_stock[t, f, d])
            c["true_cover"] = round(run.true_stock[t, f, d] / max(run.true_use[t, f, d], 1e-9), 1)
        return c


replay = Replay(int(os.environ.get("ANUMAAN_SEED", 5)))   # a held-out seed: never used for tuning
app = FastAPI(title="Anumaan")
DAYS = replay.run.book.shape[0]


def _day(t):
    if not 0 <= t < DAYS:
        raise HTTPException(404, f"day must be in 0..{DAYS - 1}")
    return t


def _cell_ok(f, j):
    if not (0 <= f < len(replay.run.facilities) and 0 <= j < len(replay.drugs)):
        raise HTTPException(404, "no such facility/medicine")


def _num(x):
    """JSON-safe float: NaN (e.g. no forecast band yet) becomes null."""
    return None if x is None or np.isnan(x) else round(float(x), 1)


@app.get("/api/meta")
def meta():
    run, crg = replay.run, G.load()
    ph = np.array([[[replay.phantom(t, f, j) for j in range(len(replay.drugs))]
                    for f in range(len(run.facilities))] for t in range(DAYS)])
    t0 = int(ph.sum((1, 2)).argmax())
    f0, j0 = map(int, np.argwhere(ph[t0])[0]) if ph[t0].any() else (0, 0)
    return dict(days=DAYS, synthetic=True, grammar=crg["version"], grammar_note=crg["source_note"],
                start=dict(day=t0, f=f0, j=j0), horizon=HORIZON, low=FL.LOW,
                facilities=[dict(id=i, wh=w, st=s) for i, w, s in zip(run.facilities, run.wh, run.st)],
                drugs=[dict(name=run.ix["drugs"][d], unit=crg["drugs"][run.ix["drugs"][d]]["unit"]) for d in replay.drugs],
                cadres=CARE.CADRES, levels=triage.LEVELS, actions=triage.ACTION)


@app.get("/api/day/{t}")
def day(t: int, truth: bool = False):
    labels = replay.labels(_day(t))
    cells = [replay.cell(t, f, j, labels, truth)
             for f in range(len(replay.run.facilities)) for j in range(len(replay.drugs))]
    summary = {k: sum(c["regime"] == k for c in cells) for k in FL.REGIMES}
    summary.update(phantom=sum(c["phantom"] for c in cells),
                   **{lv: sum(c.get("level") == lv for c in cells) for lv in triage.LEVELS})
    return dict(day=t, cells=cells, summary=summary)


@app.get("/api/series/{f}/{j}")
def series(f: int, j: int, t: int, truth: bool = False):
    _cell_ok(f, j)
    run, obs = replay.run, replay.obs
    d, sl = replay.drugs[j], slice(0, _day(t) + 1)
    out = {k: obs[k][sl, f, d].round(2).tolist() for k in ("N", *G.CATS)}
    out.update(book=run.book[sl, f, d].round().tolist(), use=replay.use[sl, f, j].round(1).tolist(),
               receipts=run.receipts[sl, f, d].round().tolist(), gap=replay.skip[f][sl].tolist(),
               p_scarce=replay.post[sl, f, j, 1].round(3).tolist(), p_out=replay.post[sl, f, j, 2].round(3).tolist())
    if truth:
        out["true"] = run.true_stock[sl, f, d].round().tolist()
    return out


@app.get("/api/forecast/{f}/{j}")
def forecast(f: int, j: int, t: int):
    """Next HORIZON days of units: from diagnoses through the CRG, and the fair
    consumption baseline (dispensing with out-days filled from the in-stock rate)."""
    _cell_ok(f, j)
    _day(t)
    obs, d = replay.obs, replay.drugs[j]
    mean, lo, hi = FC.forecast_series(obs["exp_units"][:t + 1, f, d], HORIZON)
    adj = FC.adjust_consumption(obs["units"][:, f, d], replay.out_mask[:, f, d], t)
    cons = FC.forecast_series(adj[:t + 1], HORIZON)[0]
    return dict(days=list(range(t + 1, t + 1 + HORIZON)), mean=[_num(x) for x in mean],
                lo=[_num(x) for x in lo], hi=[_num(x) for x in hi],
                total=round(float(mean.sum())), consumption_total=round(float(cons.sum())))


@app.get("/api/facility/{f}")
def facility(f: int, t: int, truth: bool = False):
    """Beds from the ADT feed and staff from attendance + acts, for one PHC on day t."""
    _cell_ok(f, 0)
    c, b, s = replay.care, replay.beds, replay.staff
    early = b["early"][_day(t), f]
    out = dict(beds=dict(capacity=int(c.capacity[f]), occupied=int(b["occupied"][t, f]),
                         free=int(c.capacity[f] - b["occupied"][t, f]), pressure=bool(b["pressure"][t, f]),
                         early_share_7d=None if np.isnan(early) else round(float(early), 2)),
               staff=[dict(cadre=k, sanctioned=int(CARE.SANCTIONED[i]), in_position=int(c.in_position[f, i]),
                           marked_present=bool(c.marked[t, f, i]), p_present=round(float(s["p_present"][t, f, i]), 2),
                           acts=int(c.acts[t, f, i]), expected=round(float(s["expected"][t, f, i]), 1),
                           verify=bool(s["verify"][t, f, i]))
                      for i, k in enumerate(CARE.CADRES)])
    if truth:
        out["beds"]["true_occupied"] = int(c.occupancy[t, f])
        for i, row in enumerate(out["staff"]):
            row["true_present"] = bool(c.present[t, f, i])
    return out


@app.get("/api/plan")
def plan(t: int):
    """Redistribution for day t: same-state transfers in treatment courses (OR-Tools
    min-cost flow), escalations where moving stock cannot help, DVDMS-style orders."""
    run, obs = replay.run, replay.obs
    labels = PL.labels_at(_day(t), run, replay.post_d, obs, replay.cover_d)
    return PL.plan(t, run, replay.post_d, labels, replay.coords, horizon=HORIZON, obs=obs, cover=replay.cover_d)


@app.get("/api/national")
def national(t: int):
    """What the national project sees: each state's export through the clean-room gate."""
    nat = FED.National()
    exports = [n.export(_day(t)) for n in replay.nodes]
    for ex in exports:
        nat.ingest(ex)                  # raises if an export carries anything raw
    return dict(exports=exports, view=nat.view(), priors=nat.priors(), raw_rows=replay.raw_rows,
                export_bytes={ex["state"]: len(json.dumps(ex)) for ex in exports})


class Confirm(BaseModel):
    f: int
    j: int
    t: int
    answer: Literal["empty", "available"]


def _apply(f, j, t, answer):
    replay.confirm.setdefault((f, j), {})[t] = answer
    replay.refilter(f, j)
    return replay.post[t, f, j].round(3).tolist()


@app.post("/api/confirm")
def confirm(c: Confirm):
    """A pharmacist's shelf check becomes evidence for that day; only this series is re-filtered."""
    _cell_ok(c.f, c.j)
    return dict(ok=True, p=_apply(c.f, c.j, _day(c.t), c.answer))


@app.post("/api/voice")
async def voice_confirm(request: Request, f: int, j: int, t: int, language: str | None = None):
    """A spoken shelf check: Gemini transcribes and labels it; a clear yes/no updates the
    estimate exactly like the buttons. 503 when Gemini is not configured."""
    _cell_ok(f, j)
    _day(t)
    today = date.today()
    if voice_used.get(today, 0) >= VOICE_CAP:
        raise HTTPException(429, "Voice answers are used up for today on this demo. Use the buttons.")
    voice_used[today] = voice_used.get(today, 0) + 1
    audio = await request.body()
    mime = request.headers.get("content-type", "audio/webm")
    try:   # label_voice blocks for up to 30 s; keep it off the event loop
        heard = await run_in_threadpool(voice.label_voice, audio, mime,
                                        replay.run.ix["drugs"][replay.drugs[j]], language)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except voice.VoiceUnavailable as e:
        raise HTTPException(503, str(e))
    if heard["answer"] in ("empty", "available"):
        heard["p"] = _apply(f, j, t, heard["answer"])
    return heard


@app.get("/api/eval")
def eval_():
    m = replay.metrics
    out = {k: v for k, v in m.items() if not k.startswith("triage_") and k != "types"}
    out["types"] = dict(m["types"])
    out["triage_majority"] = m["triage_majority"]
    for k in ("triage_7d", "triage_21d"):
        out[k] = {x: y for x, y in m[k].items() if x != "confusion"}
        out[k]["confusion"] = [dict(truth=a, pred=b, n=n) for (a, b), n in m[k]["confusion"].items()]
    return out


@app.get("/api/truth/episodes")
def episodes():
    run = replay.run
    return [dict(e, drug=run.ix["drugs"][e["drug"]]) for e in run.episodes]


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
