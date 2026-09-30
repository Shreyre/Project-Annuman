"""Scenario replays: the simulator with its failures scripted from a reported event, on a
real state's districts, instead of drawn at random.

Every record is still SYNTHETIC. A scenario answers "what would Anumaan show if this
happened?"; it is not the state's data and not a forecast. What is real: the district
names, the treatment guidelines behind the grammar, and the event the script follows.

The Kerala replay follows two reports (ABOUT["sources"]). Onmanorama, 22 September 2026:
at a KMSCL board meeting "companies had shown no interest in supplying 180 of the items
included in this year's tender", and those items were to be re-tendered. Onmanorama,
29 September 2026: district medical officers told the health minister that hospitals had
adequate stocks "while shortages are currently being reported at the Kerala Medical
Services Corporation Limited (KMSCL) warehouses". Warehouses short first, PHC shelves
after: that is the pattern scripted here. KMSCL has not published which items drew no
bids, so the three medicines below are this project's tracers, not the tender's items.

    python -m anumaan.scenario          # the Kerala replay's alarms, week by week
"""
import numpy as np

from anumaan import crg as G, filter as FL, sim, triage

# Kerala's 14 districts and their headquarters (approximate). The replay gives each one warehouse;
# KMSCL lists 17 sites, two each in Thiruvananthapuram, Thrissur and Malappuram.
KERALA = (("Thiruvananthapuram", 8.52, 76.94), ("Kollam", 8.89, 76.61), ("Pathanamthitta", 9.27, 76.79),
          ("Alappuzha", 9.50, 76.34), ("Kottayam", 9.59, 76.52), ("Idukki", 9.85, 76.97),
          ("Ernakulam", 10.02, 76.34), ("Thrissur", 10.53, 76.21), ("Palakkad", 10.79, 76.65),
          ("Malappuram", 11.05, 76.07), ("Kozhikode", 11.26, 75.78), ("Wayanad", 11.61, 76.08),
          ("Kannur", 11.87, 75.37), ("Kasaragod", 12.50, 74.99))
SEED, DAYS, PHCS = 26, 150, 5    # 5 PHCs a district: the federation's gate suppresses any group under five
BREAK = 35                       # the day the state stops filling warehouse indents for the tendered medicines
OPEN = 70                        # where the UI opens: the verdict is in, most shelves have not emptied yet
# What the UI says above this replay; the numbers in SCRIPT are measured on the run when the app builds it.
ABOUT = dict(
    title="Kerala replay: when the state's own supply breaks",
    event="What it follows: in September 2026 Onmanorama reported that companies had shown no interest in supplying "
          "180 of the items in KMSCL's medicine tender for the year, and a week later that hospitals still had stock "
          "while KMSCL's warehouses were running short.",
    note="Every record here is synthetic: 70 simulated PHCs, five in each of Kerala's 14 districts, with one warehouse "
         "a district. It is a what-if, not KMSCL's data and not a forecast. The three medicines are this project's "
         "tracers: KMSCL has not published which items drew no bids.",
    sources=[dict(label="Onmanorama, 22 September 2026",
                  url="https://www.onmanorama.com/news/kerala/2026/09/22/kmscl-supply-crisis-delays-hospitals-medicine-scarcity-kerala.html"),
             dict(label="Onmanorama, 29 September 2026",
                  url="https://www.onmanorama.com/news/kerala/2026/09/29/medicine-stocks-available-at-hospitals-shortage-at-kmscl-warehouses-dmos-inform-health-minister.html")])
SCRIPT = ("On day {start} the state stops filling warehouse indents for amoxicillin, metformin and amlodipine. "
          "By day {called} the warehouse ledger shows it in {starved} of 14 districts and Anumaan calls a state "
          "procurement failure: escalate, because moving stock between PHCs cannot fix it. The first PHC shelf "
          "empties on day {first}, and {after} stock-outs it goes on to cause come after the call.")
# The state-level failure, on three tracer medicines, and the ordinary trouble it has to be told from.
EPISODES = (
    dict(type="STATE-PROCUREMENT", root="S0", drug="amoxicillin_500", start=BREAK, dur=DAYS, short=0.05),
    dict(type="STATE-PROCUREMENT", root="S0", drug="metformin_500", start=BREAK, dur=DAYS, short=0.10),
    dict(type="STATE-PROCUREMENT", root="S0", drug="amlodipine_5", start=BREAK + 7, dur=DAYS, short=0.10),
    dict(type="WAREHOUSE", root="S0-W5", drug="ors_sachet", start=50, dur=45, short=0.0),
    dict(type="LOCAL", root="S0-W7-P2", drug="paracetamol_500", start=60, dur=30),
    dict(type="LOCAL", root="S0-W11-P0", drug="ifa_tab", start=80, dur=35),
)


def kerala(seed=SEED):
    """(run, coords [F, 2], names): one warehouse in each of 14 districts x PHCS synthetic PHCs over DAYS days.
    Warehouses hold 20-50 days, so PHC shelves start to empty three to seven weeks after BREAK.
    Coordinates: each PHC within 12 km of its district headquarters (SYNTHETIC points)."""
    run = sim.simulate(seed=seed, days=DAYS, n_states=1, n_wh=len(KERALA), n_phc=PHCS, wh_days=(20, 50),
                       episodes=EPISODES)
    rng = np.random.default_rng(seed)
    c = np.array([KERALA[int(w.split("-W")[1])][1:] for w in run.wh])
    km, ang = 12 * np.sqrt(rng.random(len(c))), rng.uniform(0, 2 * np.pi, len(c))
    coords = c + np.column_stack([km * np.sin(ang) / 111, km * np.cos(ang) / (111 * np.cos(np.radians(c[:, 0])))])
    names = dict(states={"S0": "Kerala"}, warehouses={f"S0-W{i}": f"{d} warehouse" for i, (d, _, _) in enumerate(KERALA)})
    return run, coords, names


def main():
    run, _, names = kerala()
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    P = run.ix["primaries"]
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    segs = {}
    for f in range(len(run.facilities)):
        skip = FL.entry_gaps(given[:, f], obs["N"][:, f, P].sum(1))
        for d in P:
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            N, rec, book, exp = obs["N"][:, f, d], run.receipts[:, f, d], run.book[:, f, d], obs["exp_units"][:, f, d]
            rho, tau = FL.learn(N, cats, book, exp, rec, skip)
            post = FL.filter_series(N, cats, book, exp, rec, skip, tau=tau, rho=rho)
            cover = FL.shadow_cover(book, rec, obs["units"][:, f, d], FL.trailing_mean(rho * exp), post, skip)
            segs[(f, d)] = FL.alarms(post, cover=cover)
    fill, lift = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted), triage.surge_lift(obs["N"], run.st)
    print(f"SYNTHETIC Kerala replay: {len(run.facilities)} PHCs under {len(KERALA)} district warehouses, {DAYS} days; "
          f"state supply of 3 medicines breaks on day {BREAK}")
    print(f"{'day':>4}{'warehouses starved':>20}{'PHC alarms':>12}  where it broke")
    for t in range(28, DAYS, 7):
        live = [(f, d, s) for (f, d), ss in segs.items() for s, e in ss if s + 1 <= t < e]
        levels = triage.classify(live, run.wh, run.st, fill, lift, t)
        starved = int(triage.starved(fill, t - triage.LOOK, t)[:, P].any(1).sum())
        print(f"{t:>4}{starved:>20}{len(live):>12}  " + ", ".join(f"{k} {levels.count(k)}" for k in triage.LEVELS if k in levels))


if __name__ == "__main__":
    main()
