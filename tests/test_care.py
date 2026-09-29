from anumaan import care as CR, sim


def test_care_views_beat_the_raw_feeds_on_a_held_out_network():
    c = CR.simulate(sim.simulate(seed=5), seed=5)        # VERIFY / BUSY chosen on seeds 0-4
    r = CR.evaluate_care(c)
    assert r["mae_auto"] < r["mae_naive"] / 5             # missing discharges drift the naive count (both capped)
    assert r["full_auto"]["precision"] > r["full_naive"]["precision"] + 0.3
    assert r["feed_missed"] > 0.15                        # proxy marking hides real absences...
    assert r["ghost"]["precision"] >= 0.85 and r["ghost"]["recall"] >= 0.6   # ...acts expose them
    assert r["ghost"]["precision"] >= r["ghost_ratio"]["precision"] - 0.02   # no worse than the fair no-model rule
    c.occupancy = c.present = None                        # the views never read GROUND TRUTH
    assert (CR.beds(c)["occupied"] <= c.capacity).all()   # a 6-bed ward never shows 10 patients
    assert len(CR.beds_view(c, 100)) == len(CR.staff_view(c, 100)) / len(CR.CADRES) == len(c.facilities)


def test_empty_opd_is_not_absence():
    c = CR.simulate(sim.simulate(seed=5), seed=5)
    t, row = 100, 0                                       # facility 0, MO (always someone in post)
    c.marked[t, 0, 0], c.acts[t, 0, 0] = True, 0
    c.exposure[t, 0, 0] = 0                               # nobody came: no evidence either way
    quiet = CR.staff_view(c, t)[row]
    c.exposure[t, 0, 0] = 40                              # 40 patients, not one MO prescription
    busy = CR.staff_view(c, t)[row]
    assert quiet["p_present"] > 0.5 and not quiet["verify"]
    assert busy["p_present"] < 0.1 and busy["verify"]
