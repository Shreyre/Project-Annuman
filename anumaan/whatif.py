"""What-if worlds: pick an event, and the simulator plays it out on synthetic records for Anumaan
to read like any other network.

    What-if on synthetic records: not a forecast and not a test set.

Two worlds. "kerala": the Kerala replay's 70 PHCs in 14 districts, keeping its ordinary trouble
(scenario.BACKGROUND) with the viewer's event in place of its state failure. "demo": the demo
network's 36 PHCs in 2 states, keeping its own failures. Three events, on a day from 35 to 110
(after the 30-day warm-up the filter learns from, and 40 or more days before the end):
  warehouse   a district warehouse runs out of one medicine and the state stops refilling it
  state       the state (S0) stops filling every warehouse's indents for one medicine
  emergency   fever and acute diarrhoea diagnoses in one district x2 to x4, for 28 days
A world is not its base plus the event: the simulator's random draws part once the scripts
differ, so read it on its own, not as a with/without comparison.

    python -m anumaan.whatif            # the menu's picks: their story and build time
"""
import time
from collections import Counter
from functools import cache

from anumaan import crg as G, planner as PL, scenario, sim, triage

LABEL = "What-if on synthetic records: not a forecast and not a test set."
FIRST, LAST = 35, 110            # event day: after the 30-day warm-up, and 40 or more days before the end
LIFTS = (2, 2.5, 3, 3.5, 4)      # an emergency multiplies its diagnoses by one of these
CONDS, SPELL = ("fever", "acute_diarrhoea"), 28
SHORT = 0.05                     # a cut still lets 5% through, like the Kerala script's amoxicillin
DEMO_SEED = 5                    # the demo network's held-out seed (app.main's default)
DAYS = dict(kerala=scenario.DAYS, demo=200)     # the demo network: sim.simulate's defaults, 2 states x 3 warehouses
DISTRICTS = dict(kerala=tuple(f"S0-W{i}" for i in range(len(scenario.KERALA))),
                 demo=tuple(f"S{s}-W{w}" for s in range(2) for w in range(3)))
FIELDS = dict(warehouse=("district", "medicine", "day"), state=("medicine", "day"),
              emergency=("district", "lift", "day"))
LEVEL = dict(warehouse="WAREHOUSE", state="STATE-PROCUREMENT", emergency="DEMAND-SURGE")
_ix = G.index(G.load())
MEDICINES = tuple(_ix["drugs"][d] for d in _ix["primaries"])


def validate(params):
    """The world's key, e.g. "whatif:kerala:warehouse:S0-W6:amoxicillin_500:40". params: world, event
    and that event's FIELDS; district is a warehouse id (S0-W6), day and lift may come as strings
    from a query. Anything else raises ValueError."""
    world, event = (params.get("world"), params.get("event")) if isinstance(params, dict) else (None, None)
    if not isinstance(world, str) or not isinstance(event, str) or world not in DISTRICTS or event not in FIELDS:
        raise ValueError(f"world must be one of {', '.join(DISTRICTS)} and event one of {', '.join(FIELDS)}")
    if set(params) != {"world", "event", *FIELDS[event]}:
        raise ValueError(f"a {event} what-if takes {', '.join(FIELDS[event])}")
    key = ["whatif", world, event]
    for k in FIELDS[event]:
        v = params[k]
        if k == "district" and v not in DISTRICTS[world]:
            raise ValueError(f"district must be one of {', '.join(DISTRICTS[world])}")
        if k == "medicine" and v not in MEDICINES:
            raise ValueError(f"medicine must be one of {', '.join(MEDICINES)}")
        if k == "day":
            if isinstance(v, bool) or not (isinstance(v, int) or isinstance(v, str) and v.isascii() and v.isdigit()) \
                    or not FIRST <= int(v) <= LAST:
                raise ValueError(f"day must be a whole number from {FIRST} to {LAST}")
            v = int(v)
        if k == "lift":
            try:
                v = float(v) if not isinstance(v, bool) else None
            except (TypeError, ValueError):
                v = None
            if v not in LIFTS:
                raise ValueError(f"lift must be one of {', '.join(map(str, LIFTS))}")
            v = f"{v:g}"
        key.append(str(v))
    return ":".join(key)


def parse(key):
    """validate's inverse: the params of a what-if key. One spelling per world, so a cache never
    holds the same world twice; anything else raises ValueError."""
    parts = key.split(":") if isinstance(key, str) else []
    fields = FIELDS.get(parts[2]) if len(parts) > 2 and parts[0] == "whatif" else None
    if not fields or len(parts) != 3 + len(fields):
        raise ValueError(f"not a what-if key: {key!r}")
    params = dict(world=parts[1], event=parts[2], **dict(zip(fields, parts[3:])))
    if validate(params) != key:
        raise ValueError(f"not a what-if key: {key!r}")
    return params


MENU = tuple(dict(label=label, key=validate(p)) for label, p in (
    ("Ernakulam warehouse runs out of amoxicillin on day 40",
     dict(world="kerala", event="warehouse", district="S0-W6", medicine="amoxicillin_500", day=40)),
    ("A health emergency in Ernakulam: fever and diarrhoea x3 from day 60",
     dict(world="kerala", event="emergency", district="S0-W6", lift=3, day=60)),
    ("The demo network's State 1 stops supplying amoxicillin on day 40",
     dict(world="demo", event="state", medicine="amoxicillin_500", day=40))))


def choices():
    """What the app's form offers: districts by world (id -> name), medicines, lifts, days, the menu."""
    return dict(label=LABEL, days=[FIRST, LAST], lifts=list(LIFTS), medicines=list(MEDICINES), menu=list(MENU),
                districts={w: {d: _where(w, d)[0 if w == "kerala" else 1] for d in ds} for w, ds in DISTRICTS.items()})


@cache
def _demo_failures():
    """The demo network's own failures as a script (the substitutes that fail with them are drawn again)."""
    run = sim.simulate(seed=DEMO_SEED, days=DAYS["demo"])
    return tuple(dict(e, drug=run.ix["drugs"][e["drug"]]) for e in run.episodes if not e.get("secondary"))


def _med(name):
    """amoxicillin_500 -> amoxicillin, ors_sachet -> ORS, as the UI writes them."""
    return " ".join(w.upper() if len(w) <= 3 else w for w in name.rsplit("_", 1)[0].split("_"))


def _where(world, root):
    """(district, warehouse, state) in words for a state, warehouse or PHC id, as the UI names them."""
    s, _, w = root.partition("-")
    state = "Kerala" if world == "kerala" else f"State {int(s[1:]) + 1}"
    if not w:
        return None, None, state
    w = int(w.split("-")[0][1:])
    if world == "kerala":
        return scenario.KERALA[w][0], f"{scenario.KERALA[w][0]} warehouse", state
    wh = f"Warehouse {chr(65 + w)} in {state}"
    return f"the district of {wh}", wh, state


def _told(world, e):
    """One scripted failure in words, e.g. "Idukki warehouse out of ORS from day 50 for 45 days"."""
    med, when = _med(e["drug"]), f"from day {e['start']} for {e['dur']} days"
    if e["type"] == "NATIONAL":
        return f"{med} short nationally {when}"
    district, wh, state = _where(world, e["root"])
    who = {"STATE-PROCUREMENT": state, "WAREHOUSE": wh, "LOCAL": f"one PHC in {district}"}[e["type"]]
    return f"{who} out of {med} {when}"


def build(params):
    """(run, coords, names, about) for app.main.Replay(run, about["whatif"]["seed"], coords=coords,
    names=names). about has scenario.ABOUT's shape plus the script, and "whatif": the numbers behind
    the script and "start", the {day, f, j} the UI should open on. Raises ValueError on bad params."""
    key = validate(params)
    p = parse(key)
    world, event, day = p["world"], p["event"], int(p["day"])
    root = "S0" if event == "state" else p["district"]
    extra, surges = (), None
    if event == "emergency":
        surges = [dict(root=root, conds=CONDS, lift=float(p["lift"]), start=day, dur=SPELL)]
    else:
        extra = (dict(type=LEVEL[event], root=root, drug=p["medicine"], start=day, dur=DAYS[world] - day,
                      short=SHORT),)      # to the end of the run
    if world == "kerala":
        background = scenario.BACKGROUND
        run, coords, names = scenario.kerala(episodes=background + extra, surges=surges)
        seed, note = scenario.SEED, scenario.PLACES
        base = "the Kerala replay's 70 PHCs in 14 districts, with its ordinary trouble"
    else:
        background = _demo_failures()
        run = sim.simulate(seed=DEMO_SEED, days=DAYS["demo"], episodes=background + extra, surges=surges)
        coords, names, seed = PL.synthetic_coords(run, DEMO_SEED), {}, DEMO_SEED
        note = "Every record here is synthetic: the demo network's 36 simulated PHCs in 2 made-up states."
        base = "the demo network's 36 PHCs in 2 states, with its own failures"

    # the story, measured as app.main._kerala measures the Kerala replay's: the app's alarms and labels
    # in the event's area (on its medicine, for a cut), against GROUND TRUTH stock-outs
    ix, T, level = run.ix, run.book.shape[0], LEVEL[event]
    area = {f for f in range(len(run.facilities)) if root in (run.wh[f], run.st[f])}
    drugs = list(ix["primaries"]) if event == "emergency" else [ix["di"][p["medicine"]]]
    obs = G.aggregate(ix, run.dx, run.slips, run.na)
    segs = scenario.alarm_spells(run, obs, sorted(area), drugs)
    fill = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted)
    lift, dlift = triage.surge_lift(obs["N"], run.st), triage.surge_lift(obs["N"], run.wh)

    def labels(t, onsets):
        return dict(zip(onsets, triage.classify(onsets, run.wh, run.st, fill, lift, t, dlift)))

    live = lambda t: [(f, d, s) for (f, d), ss in segs.items() for s, e in ss if s + 1 <= t < e]
    called = next((t for t in range(day, T) if level in labels(t, live(t)).values()), None)
    outs = sorted(e["out"] for e in sim.stockout_events(run) if e["fac"] in area and e["type"] == level
                  and e["drug"] in drugs and day <= e["out"] < (day + SPELL if event == "emergency" else T))
    first = outs[0] if outs else None
    after = sum(o > called for o in outs) if called is not None else 0
    since = [(f, d, s) for (f, d), ss in segs.items() for s, _ in ss if day <= s < T - 1]
    final = Counter(labels(T - 1, since).values())            # how the area's alarms since the event read at the end
    open_day = called if called is not None else first if first is not None else T - 1
    now = labels(open_day, live(open_day))
    f0, d0, _ = next((k for k, v in now.items() if v == level), next(iter(now), (min(area), drugs[0], 0)))

    district, wh, state = _where(world, root)
    med = _med(p["medicine"]) if event != "emergency" else None
    there = "" if event == "state" else " there"
    if event == "warehouse":
        title = f"What if {wh} runs out of {med}?"
        what = f"On day {day} {wh} runs out of {med} and the state stops refilling it."
    elif event == "state":
        title = f"What if {state} stops supplying {med}?"
        what = f"On day {day} {state} stops filling its warehouses' indents for {med}."
    else:
        title = f"What if a health emergency hits {district}?"
        what = (f"On day {day} a health emergency in {district} multiplies fever and acute diarrhoea diagnoses by "
                f"{p['lift']} for {SPELL} days.")
    words = dict(warehouse="a warehouse failure", state="a state procurement failure", emergency="a demand surge")[event]
    action, n = triage.ACTION[level], len(outs)
    caused = "during the emergency" if event == "emergency" else "it goes on to cause"
    if called is None:
        read = ", ".join(f"{k} {c}" for k, c in final.most_common())
        verdict = f"By day {T - 1} Anumaan has not called {words}{there}" + (
            f"; its alarms{there} since day {day} read {read}." if final else ".")
    elif called == day and level in labels(day - 1, live(day - 1)).values():     # e.g. the monsoon got there first
        verdict = f"Anumaan already shows {words}{there} the day before: {action}."
    elif event == "state":
        mine = [w for w, x in enumerate(sorted(set(run.wh))) if x.startswith(root + "-")]
        starved = int(triage.starved(fill, called - triage.LOOK, called)[mine, drugs[0]].sum())
        verdict = (f"By day {called} the warehouse ledger shows it in {starved} of {len(mine)} districts and Anumaan "
                   f"calls {words}: {action}.")
    else:
        verdict = f"Anumaan calls {words}{there} on day {called}: {action}."
    if not outs:
        shelves = f"No PHC shelf{there} empties " + (f"{caused}." if event == "emergency" else f"from it by day {T - 1}.")
    elif called is None:
        shelves = (f"The one stock-out {caused} empties a PHC shelf{there} on day {first}." if n == 1 else
                   f"The first of the {n} stock-outs {caused} empties a PHC shelf{there} on day {first}.")
    else:
        shelves = f"The first PHC shelf{there} empties on day {first}, and " + (
            f"the one stock-out {caused} comes {'after' if after else 'no later than'} the call." if n == 1 else
            f"{'all' if after == n else after or 'none'} of the {n} stock-outs {caused} come after the call.")
    about = dict(
        title=title, sources=[], script=f"{what} {verdict} {shelves}", note=f"{LABEL} {note}",
        event=f"The event is this pick, played out by the simulator on {base}: "
              + "; ".join(_told(world, e) for e in background) + ".",
        whatif=dict(key=key, world=world, event=event, district=p.get("district"), medicine=p.get("medicine"),
                    lift=float(p["lift"]) if "lift" in p else None, day=day, level=level, called=called, first=first,
                    stockouts=len(outs), after=after, labels=dict(final), seed=seed,
                    start=dict(day=open_day, f=int(f0), j=list(ix["primaries"]).index(d0))))
    return run, coords, names, about


def main():
    print(LABEL)
    for m in MENU:
        t0 = time.perf_counter()
        _, _, _, about = build(parse(m["key"]))
        print(f"\n{m['key']}  (built in {time.perf_counter() - t0:.2f} s)\n  {about['script']}")


if __name__ == "__main__":
    main()
