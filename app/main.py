"""Anumaan demo service: replays a SYNTHETIC PHC network through the filter and
serves the Counter Truth and supply-tree views. One container: API + static UI.

    uvicorn app.main:app --reload
"""
import os
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from anumaan import crg as G, filter as FL, sim, triage
from anumaan.evaluate import evaluate

THR = 0.7


class Replay:
    """Everything the UI can ask for, precomputed. The filter is causal, so the
    view at day t only ever reflects data up to day t."""

    def __init__(self, seed):
        self.run = run = sim.simulate(seed=seed)
        self.obs = obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
        self.drugs = P = list(run.ix["primaries"])
        T, F, J = run.book.shape[0], len(run.facilities), len(P)
        given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
        self.skip = [FL.entry_gaps(given[:, f], obs["N"][:, f, P].sum(1)) for f in range(F)]
        self.post = np.zeros((T, F, J, 3))
        self.use = np.stack([FL.trailing_mean(obs["exp_units"][:, f, d]) for f in range(F) for d in P], 1).reshape(T, F, J)
        self.onset = np.full((T, F, J), -1)
        self.segs, self.confirm = {}, {}
        for f in range(F):
            for j in range(J):
                self.refilter(f, j)
        self.metrics = evaluate(run)

    def refilter(self, f, j):
        run, obs, d = self.run, self.obs, self.drugs[j]
        cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
        p = FL.filter_series(obs["N"][:, f, d], cats, run.book[:, f, d], obs["exp_units"][:, f, d],
                             run.receipts[:, f, d], self.skip[f], confirm=self.confirm.get((f, j)))
        self.post[:, f, j] = p
        self.segs[(f, j)] = segs = FL.alarms(p, THR)
        self.onset[:, f, j] = -1
        for s, e in segs:
            self.onset[s + 1:e, f, j] = s     # an alarm needs 2 days: live from s+1

    def phantom(self, t, f, j):
        return self.onset[t, f, j] >= 0 and self.post[t, f, j, 2] >= THR and \
            self.run.book[t, f, self.drugs[j]] >= 7 * max(self.use[t, f, j], 1e-9)

    def labels(self, t):
        run, obs = self.run, self.obs
        known = [(f, j, s) for (f, j), segs in self.segs.items() for s, _ in segs if s + 1 <= t]
        surge = {(f, j, s) for f, j, s in known
                 if triage.demand_led(obs["N"][:, f, self.drugs[j]], run.receipts[:, f, self.drugs[j]], s + 1)}
        return dict(zip(known, triage.classify(known, run.wh, run.st, settle=t, surge=surge)))

    def cell(self, t, f, j, labels, truth):
        run, d = self.run, self.drugs[j]
        p = self.post[t, f, j]
        book = run.book[t, f, d]
        c = dict(f=f, j=j, book=round(book), cover=round(book / max(self.use[t, f, j], 1e-9), 1),
                 p=[round(float(x), 3) for x in p], regime=FL.REGIMES[int(p.argmax())],
                 alarm=bool(self.onset[t, f, j] >= 0), phantom=bool(self.phantom(t, f, j)),
                 confirmed=(self.confirm.get((f, j)) or {}).get(t))
        if c["alarm"]:
            s = int(self.onset[t, f, j])
            c.update(onset=s, level=labels[(f, j, s)], action=triage.ACTION[labels[(f, j, s)]])
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


@app.get("/api/meta")
def meta():
    run, crg = replay.run, G.load()
    ph = np.array([[[replay.phantom(t, f, j) for j in range(len(replay.drugs))]
                    for f in range(len(run.facilities))] for t in range(DAYS)])
    t0 = int(ph.sum((1, 2)).argmax())
    f0, j0 = map(int, np.argwhere(ph[t0])[0]) if ph[t0].any() else (0, 0)
    return dict(days=DAYS, synthetic=True, grammar=crg["version"], grammar_note=crg["source_note"],
                start=dict(day=t0, f=f0, j=j0),
                facilities=[dict(id=i, wh=w, st=s) for i, w, s in zip(run.facilities, run.wh, run.st)],
                drugs=[dict(name=run.ix["drugs"][d], unit=crg["drugs"][run.ix["drugs"][d]]["unit"]) for d in replay.drugs],
                levels=triage.LEVELS, actions=triage.ACTION)


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


class Confirm(BaseModel):
    f: int
    j: int
    t: int
    answer: Literal["empty", "available"]


@app.post("/api/confirm")
def confirm(c: Confirm):
    """A pharmacist's shelf check becomes evidence for that day; only this series is re-filtered."""
    _cell_ok(c.f, c.j)
    replay.confirm.setdefault((c.f, c.j), {})[_day(c.t)] = c.answer
    replay.refilter(c.f, c.j)
    return dict(ok=True, p=replay.post[c.t, c.f, c.j].round(3).tolist())


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
