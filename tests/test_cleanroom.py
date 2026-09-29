import re

from anumaan import cleanroom as CR, federation as FED, filter as FL, sim


def test_the_shared_counts_are_the_state_export():
    # shared_sql's GROUP BY and HAVING, done in Python over the private rows, must give export(t)
    # back, suppression included
    run = sim.simulate(seed=5)
    rows = CR.phc_rows(run, "S0")
    assert len(rows) == 18 * len(run.ix["primaries"]) * run.book.shape[0]
    groups = {}
    for r in rows:
        groups.setdefault((r["warehouse"], r["drug"], r["day"]), []).append(r)
    for k in (FED.K, 7):                                   # 6 PHCs per warehouse: k=7 drops them all
        agg = [dict(warehouse=w, drug=d, day=t, n=len({r["phc"] for r in g}),
                    **{x: sum(r["regime"] == x for r in g) for x in FL.REGIMES},
                    alarm=sum(r["alarm"] for r in g), surge=sum(r["surge"] for r in g),
                    onsets=sum(r["onset"] for r in g), starved=any(r["starved"] for r in g))
               for (w, d, t), g in groups.items() if len({r["phc"] for r in g}) >= k]
        node = FED.StateNode(run, "S0", k)
        for t in (0, 45, 120, 199):
            assert CR.export_rows(agg, t) == node.export(t)["rows"]


def test_the_view_shares_counts_only():
    sql = CR.shared_sql("S2", "p")
    assert sql.startswith("SELECT warehouse, drug, day, ")                  # no per-PHC column out
    assert sql.endswith(f"HAVING COUNT(DISTINCT phc) >= {FED.K}")
    assert set(re.findall(r" AS `?(\w+)", sql)) == {"n", *FL.REGIMES, "alarm", "surge", "onsets", "starved"}
    assert "FROM `p.anumaan_state_s2.phc_daily`" in sql
    assert CR.view_sql("S2", "p").startswith("CREATE OR REPLACE VIEW `p.anumaan_state_s2_shared.warehouse_day`")
