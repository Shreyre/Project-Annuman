"""Clean room: the federation boundary (federation.py) on BigQuery.

Each state keeps its per-PHC daily rows in its own dataset, which only the state can read.
It shares one view over them from a second dataset: StateNode.export's rows, computed by
BigQuery inside the state. Per warehouse x medicine x day, the view counts the PHCs that are
OK / SCARCE / OUT, in alarm, surge-led or starting an alarm, and says whether the warehouse
is starved; a group of fewer than 5 PHCs is dropped. The view is authorized on the private
dataset, so reading it needs no role on the rows. The national layer (a service account,
deploy/national.sh) is granted the views and nothing else, and keeps what it reads in its
own dataset, anumaan_national. No per-PHC row or key crosses, so nothing finer than the
export can be asked for (proof (e)).

No privacy policy: an aggregation threshold counts distinct privacy units per group, and a
view that already aggregates has no PHC left to count (its only candidate unit, the
warehouse, would need 5 per group). HAVING enforces k instead, and only the state can edit
the view. Per-PHC rows under that policy would let differencing COUNT(DISTINCT) queries
follow one PHC.

    bash deploy/national.sh                   # once: the national layer
    python -m anumaan.cleanroom onboard S0    # per state: dataset, rows, view, grant
    python -m anumaan.cleanroom proof [--day 120]

Demo: every state is two datasets in one project. In production each state runs the same
onboard in its own project (--project), so its rows never leave it, and the only outside
principal it grants is the national layer's service account. Not moved yet: export()'s model
summaries (median and IQR of rho and tau); PERCENTILE_CONT in a second view would carry them.
Everything loaded is SYNTHETIC (anumaan/sim.py).
"""
import argparse
import json
import re
import time

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession

from anumaan import crg as G, federation as FED, filter as FL, sim, triage

PROJECT, LOCATION = "anumaan-c4c", "asia-south1"      # Mumbai: the rows stay in India
NATIONAL, NAT_DS = f"anumaan-national@{PROJECT}.iam.gserviceaccount.com", "anumaan_national"
BQ = "https://bigquery.googleapis.com/bigquery/v2/projects/"
SCOPE = ["https://www.googleapis.com/auth/cloud-platform"]


def dataset(state):
    return f"anumaan_state_{state.lower()}"


def state_run(state, seed=5):
    """The SYNTHETIC world a state's rows come from: the 2-state demo network, grown only for
    a state beyond it (S2, ...)."""
    return sim.simulate(seed=seed, n_states=max(2, int(state[1:]) + 1))


def phc_rows(run, state):
    """The state's private rows, one per PHC x primary medicine x day: the CRG-decoded counts
    from its slips and register, then what its own filter made of them, per day as export()
    reads them. They never leave the state; only shared_sql's counts do."""
    node = FED.StateNode(run, state)
    obs = G.aggregate(run.ix, node.dx, node.slips, node.na)
    T = run.book.shape[0]
    starved = [triage.starved(node.fill, t - triage.LOOK, t) for t in range(T)]
    rows = []
    for i, f in enumerate(node.fac):
        w = node.whs.index(node.wh[i])
        for d in node.P:
            segs, post, surge = node.segs[(i, d)], node.post[(i, d)], node.surge[(i, d)]
            for t in range(T):
                rows.append(dict(day=t, warehouse=node.wh[i], phc=run.facilities[f], drug=node.drugs[d],
                                 expected=round(float(obs["N"][t, i, d]), 2),
                                 **{c: int(obs[c][t, i, d]) for c in G.CATS},
                                 units=round(float(obs["units"][t, i, d]), 1), book=round(float(run.book[t, f, d]), 1),
                                 received=round(float(run.receipts[t, f, d]), 1),
                                 regime=FL.REGIMES[int(post[t].argmax())],
                                 alarm=any(s < t < e for s, e in segs),     # confirmed on its 2nd day, not over
                                 onset=any(s == t for s, _ in segs),
                                 surge=any(t - FED.WINDOW <= s < t for s in surge),
                                 starved=bool(starved[t][w, d])))
    return rows


def shared_sql(state, project=PROJECT, k=FED.K):
    """StateNode.export's rows for every day, computed inside the state; groups under k PHCs
    dropped, as export() drops them."""
    cols = ", ".join(["COUNT(DISTINCT phc) AS n", *(f"COUNTIF(regime = '{r}') AS `{r}`" for r in FL.REGIMES),
                      "COUNTIF(alarm) AS alarm", "COUNTIF(surge) AS surge", "COUNTIF(onset) AS onsets",
                      "LOGICAL_OR(starved) AS starved"])
    return (f"SELECT warehouse, drug, day, {cols}\nFROM `{project}.{dataset(state)}.phc_daily`\n"
            f"GROUP BY warehouse, drug, day HAVING COUNT(DISTINCT phc) >= {k}")


def view_sql(state, project=PROJECT, k=FED.K):
    """The one thing a state shares."""
    return (f"CREATE OR REPLACE VIEW `{project}.{dataset(state)}_shared.warehouse_day`\n"
            f"OPTIONS (description = 'SYNTHETIC. {state} per warehouse x medicine x day, as StateNode.export "
            f"sends it; groups under {k} PHCs dropped')\nAS {shared_sql(state, project, k)}")


def export_rows(agg, t):
    """StateNode.export(t)["rows"], rebuilt from one state's shared_sql rows (all days)."""
    onsets = {}
    for a in agg:
        if a["day"] < t:        # an alarm counts once confirmed, the day after it began
            onsets.setdefault((a["warehouse"], a["drug"]), []).extend([a["day"]] * a["onsets"])
    rows = [dict(warehouse=a["warehouse"], drug=a["drug"], n=a["n"], **{r: a[r] for r in FL.REGIMES},
                 alarm=a["alarm"], surge=a["surge"], starved=int(a["starved"]),
                 onsets=sorted(onsets.get((a["warehouse"], a["drug"]), [])))
            for a in agg if a["day"] == t]
    return sorted(rows, key=lambda r: (r["warehouse"], r["drug"]))


# --- BigQuery over REST (google-auth only) ---

def session(national=False):
    creds, _ = google.auth.default(scopes=SCOPE)
    if national:        # act as the national layer; needs Token Creator on it (deploy/national.sh)
        creds = impersonated_credentials.Credentials(source_credentials=creds, target_principal=NATIONAL,
                                                     target_scopes=SCOPE)
    return AuthorizedSession(creds)


def _call(s, method, url, ok=(), **kw):
    r = s.request(method, url, **kw)
    if r.status_code in ok:
        return {}
    if not r.ok:
        raise RuntimeError(r.json()["error"]["message"])
    return r.json()


CAST = dict(INTEGER=int, FLOAT=float, BOOLEAN=lambda v: v == "true")


def query(s, sql, project=PROJECT):
    """Rows as typed dicts; raises with BigQuery's own message."""
    r = _call(s, "POST", f"{BQ}{project}/queries",
              json=dict(query=sql, useLegacySql=False, location=LOCATION, timeoutMs=60000))
    out = []
    while True:
        if r.get("jobComplete"):
            fields = r.get("schema", {}).get("fields", [])
            out += [{f["name"]: c["v"] if c["v"] is None else CAST.get(f["type"], str)(c["v"])
                     for f, c in zip(fields, row["f"])} for row in r.get("rows", [])]
            if not r.get("pageToken"):
                return out
        r = _call(s, "GET", f"{BQ}{project}/queries/{r['jobReference']['jobId']}",
                  params=dict(location=LOCATION, pageToken=r.get("pageToken"), timeoutMs=60000))


def _dataset(s, name, description, project=PROJECT):
    body = dict(datasetReference=dict(projectId=project, datasetId=name), location=LOCATION, description=description)
    if not _call(s, "POST", f"{BQ}{project}/datasets", ok=(409,), json=body):     # 409: already there
        _call(s, "PATCH", f"{BQ}{project}/datasets/{name}", json=dict(description=description))


def _access(s, name, edit, project=PROJECT):
    """Set a dataset's access list to edit(current list), if that changes it."""
    ds = _call(s, "GET", f"{BQ}{project}/datasets/{name}")
    new = edit(ds["access"])
    if new != ds["access"]:
        _call(s, "PATCH", f"{BQ}{project}/datasets/{name}", json=dict(access=new), headers={"If-Match": ds["etag"]})


def _load(s, table, rows, description, project=PROJECT):
    """Replace a table with rows: a batch load job, which is free (streaming is not)."""
    ds, name = table.split(".")
    types = {int: "INTEGER", float: "FLOAT", str: "STRING", bool: "BOOLEAN"}
    load = dict(destinationTable=dict(projectId=project, datasetId=ds, tableId=name), sourceFormat="CSV",
                writeDisposition="WRITE_TRUNCATE", destinationTableProperties=dict(description=description),
                schema=dict(fields=[dict(name=k, type=types[type(v)]) for k, v in rows[0].items()]))
    meta = json.dumps(dict(configuration=dict(load=load), jobReference=dict(location=LOCATION)))
    csv = "\n".join(",".join(map(str, r.values())) for r in rows)     # ids and names hold no commas
    body = (f"--anumaan\r\nContent-Type: application/json\r\n\r\n{meta}\r\n"
            f"--anumaan\r\nContent-Type: text/csv\r\n\r\n{csv}\r\n--anumaan--").encode()
    job = _call(s, "POST", f"https://bigquery.googleapis.com/upload/bigquery/v2/projects/{project}/jobs",
                params=dict(uploadType="multipart"), data=body,
                headers={"Content-Type": "multipart/related; boundary=anumaan"})
    while job["status"]["state"] != "DONE":
        time.sleep(1)
        job = _call(s, "GET", f"{BQ}{project}/jobs/{job['jobReference']['jobId']}", params=dict(location=LOCATION))
    if "errorResult" in job["status"]:
        raise RuntimeError(job["status"]["errorResult"]["message"])


def onboard(state, seed=5, project=PROJECT, national=NATIONAL):
    """One state joins: private dataset and rows, shared view, the national layer's grant.
    Re-running reloads the rows and converges the grants."""
    s, ds = session(), dataset(state)
    rows = phc_rows(state_run(state, seed), state)
    _dataset(s, ds, f"SYNTHETIC (anumaan/sim.py seed {seed}). {state}'s private per-PHC daily rows, "
                    f"readable outside the state only through the authorized view {ds}_shared.warehouse_day", project)
    _load(s, f"{ds}.phc_daily", rows, "SYNTHETIC. Per PHC x primary medicine x day: CRG-decoded counts "
                                      "and the state's own filter output", project)
    _dataset(s, f"{ds}_shared", f"SYNTHETIC. What {state} shares: its export, per warehouse x medicine x day", project)
    query(s, view_sql(state, project), project)
    view = dict(view=dict(projectId=project, datasetId=f"{ds}_shared", tableId="warehouse_day"))
    _access(s, ds, lambda acl: [e for e in acl if "view" not in e] + [view], project)   # the one view that reads the rows
    reader = dict(role="READER", userByEmail=national)
    _access(s, f"{ds}_shared", lambda acl: acl if reader in acl else acl + [reader], project)
    print(f"{state}: {len(rows):,} SYNTHETIC rows in {project}.{ds}.phc_daily (private); view "
          f"{ds}_shared.warehouse_day (groups under {FED.K} PHCs dropped), authorized on it and readable by {national}")


def attempt(s, sql):
    """A query's rows, or BigQuery's refusal as text."""
    try:
        return query(s, sql)
    except RuntimeError as e:
        return f"refused: {e}"


def proof(day=120, seed=5, project=PROJECT):
    """The plan's verification, (a)-(d), and (e) the attack that per-PHC rows allowed. Every
    query runs as the national layer; the state's own session only checks the answers."""
    nat, own = session(national=True), session()
    seen = sorted(d["datasetReference"]["datasetId"]
                  for d in _call(nat, "GET", f"{BQ}{project}/datasets").get("datasets", []))
    states = [m[1].upper() for m in map(re.compile(r"anumaan_state_(s\d+)_shared").fullmatch, seen) if m]
    if not states:
        raise SystemExit("no state has shared a view with the national layer yet")
    views = {s: f"{project}.{dataset(s)}_shared.warehouse_day" for s in states}
    table = f"{project}.{NAT_DS}.warehouse_day"
    query(nat, f"CREATE OR REPLACE TABLE `{table}` OPTIONS (description = 'SYNTHETIC. Per warehouse x medicine "
               f"x day, read from the state views') AS\n" + "\nUNION ALL\n".join(f"SELECT * FROM `{v}`" for v in views.values()))
    agg = query(nat, f"SELECT * FROM `{table}`")
    mine = {st: [a for a in agg if a["warehouse"].startswith(st + "-")] for st in states}
    print(f"SYNTHETIC clean room on BigQuery {project} ({LOCATION}), querying as {NATIONAL}\n"
          f"  datasets it can see: {', '.join(seen)}\n"
          f"  pulled {len(agg):,} rows from {len(views)} state views into {NAT_DS}.warehouse_day")

    print("\n(a) national aggregates vs StateNode.export in Python")
    runs, nodes, same, total = {}, {}, 0, 0
    for st in states:
        runs[st] = run = state_run(st, seed)
        nodes[st] = node = FED.StateNode(run, st)
        for t in range(run.book.shape[0]):
            want, got = node.export(t)["rows"], export_rows(mine[st], t)
            same, total = same + sum(w == g for w, g in zip(want, got)), total + max(len(want), len(got))
    st0 = states[0]
    node0, ex = nodes[st0], nodes[st0].export(day)["rows"]
    head = ("n", *FL.REGIMES, "alarm", "surge", "starved", "onsets")
    print(f"  {st0}, day {day}, read back from {NAT_DS}.warehouse_day (onsets: alarms begun before day {day}):")
    print(f"    {'warehouse':10}{'medicine':20}" + "".join(f"{h:>8}" for h in head) + "  = export")
    for r, w in zip(export_rows(mine[st0], day), ex):
        print(f"    {r['warehouse']:10}{r['drug']:20}" + "".join(f"{len(r[h]) if h == 'onsets' else r[h]:>8}"
                                                            for h in head) + f"  {'yes' if r == w else 'NO'}")
    print(f"  every row, days 0-{runs[st0].book.shape[0] - 1}, {len(states)} states: {same:,} of {total:,} "
          f"identical to export(t), onset lists included")

    print(f"\n(b) queries below warehouse level ({st0}, day {day})")
    v0, fac = views[st0], runs[st0].facilities
    for label, sql in [("one PHC, by name", f"SELECT * FROM `{v0}` WHERE phc = '{fac[node0.fac[0]]}'"),
                       ("one row per PHC", f"SELECT phc_key, COUNT(*) AS c FROM `{v0}` GROUP BY phc_key"),
                       ("each PHC's status", f"SELECT regime, COUNT(*) AS c FROM `{v0}` WHERE day = {day} GROUP BY regime"),
                       (f"groups of fewer than {FED.K} PHCs", f"SELECT * FROM `{v0}` WHERE n < {FED.K}")]:
        r = attempt(nat, sql)
        print(f"  {label}: {r if isinstance(r, str) else f'{len(r)} rows'}")
    strict = FED.StateNode(runs[st0], st0, k=7).export(day)
    print(f"  (every demo warehouse has 6 PHCs, so k={FED.K} drops nothing. Check, as the state: the view's SQL at "
          f"k=7 gives {len(query(own, shared_sql(st0, project, 7)))} rows; StateNode(k=7).export({day}) keeps "
          f"{len(strict['rows'])} and suppresses {strict['suppressed']})")

    print("\n(c) the national layer reads a raw state table")
    raw = f"{project}.{dataset(st0)}.phc_daily"
    print(f"  SELECT COUNT(*) AS n FROM `{raw}`\n    {attempt(nat, f'SELECT COUNT(*) AS n FROM `{raw}`')}")
    print(f"  (the state itself reads {query(own, f'SELECT COUNT(*) AS n FROM `{raw}`')[0]['n']:,} rows there, "
          f"one per PHC x medicine x day)")

    print(f"\n(d) what {NAT_DS} holds")
    for t in query(nat, f"SELECT table_name, table_type, STRING_AGG(column_name, ', ' ORDER BY ordinal_position) AS cols "
                        f"FROM `{project}.{NAT_DS}.INFORMATION_SCHEMA.TABLES` "
                        f"JOIN `{project}.{NAT_DS}.INFORMATION_SCHEMA.COLUMNS` USING (table_name) GROUP BY 1, 2"):
        print(f"  {t['table_name']} ({t['table_type']}): {t['cols']}")
    c = query(nat, f"SELECT COUNT(*) AS n_rows, COUNT(DISTINCT FORMAT('%s|%s|%d', warehouse, drug, day)) AS n_keys, "
                   f"MIN(n) AS fewest, COUNTIF(REGEXP_CONTAINS(TO_JSON_STRING(t), r'-P[0-9]')) AS ids FROM `{table}` t")[0]
    print(f"  {c['n_rows']:,} rows = {c['n_keys']:,} warehouse x medicine x day keys; fewest PHCs behind a row: "
          f"{c['fewest']}; rows holding a facility id: {c['ids']}")

    print("\n(e) the differencing attack that per-PHC rows allowed, run on the shared view")
    starts = {}      # its target, singled out by behaviour: the only PHC in its warehouse whose alarm began that day
    for (i, d), segs in node0.segs.items():
        for s, _ in segs:
            if s < day:
                starts.setdefault((node0.wh[i], d, s), []).append(i)
    short = lambda i: [node0.drugs[d] for d in node0.P if node0.post[(i, d)][day].argmax()]
    alone = [(k, v[0]) for k, v in starts.items() if len(v) == 1]
    (w, d, s), i = next((c for c in alone if short(c[1])), alone[0])
    pad = next(x for x in node0.whs if x != w)      # another warehouse's PHCs lifted every query over k
    A = f"(warehouse = '{w}' AND drug = '{node0.drugs[d]}' AND day = {s} AND onset)"
    P = f"(warehouse = '{pad}' AND drug = '{node0.drugs[d]}' AND day = 0)"
    wheres = [P, f"{A} OR {P}"]
    for dd in node0.P:
        B = f"(warehouse = '{w}' AND drug = '{node0.drugs[dd]}' AND day = {day} AND regime != 'OK')"
        wheres += [f"{B} OR {P}", f"{A} OR {B} OR {P}"]
    out = [attempt(nat, f"SELECT WITH AGGREGATION_THRESHOLD COUNT(DISTINCT phc_key) AS u FROM `{v0}` WHERE {x}")
           for x in wheres]
    refused = [r for r in out if isinstance(r, str)]
    cell = {(a["drug"], a["day"]): a for a in mine[st0] if a["warehouse"] == w}
    print(f"  its {len(out)} COUNT(DISTINCT phc_key) queries, verbatim: {len(refused)} {refused[0] if refused else ''}\n"
          f"  what the national layer has instead is the export: in {w}, {cell[(node0.drugs[d], s)]['onsets']} PHC's "
          f"{node0.drugs[d]} alarm began on day {s}; on day {day}, PHCs short of " + ", ".join(
              f"{node0.drugs[dd]} {cell[(node0.drugs[dd], day)]['SCARCE'] + cell[(node0.drugs[dd], day)]['OUT']}"
              f"/{cell[(node0.drugs[dd], day)]['n']}" for dd in node0.P) +
          f"\n  no row says which PHC, so the one whose {node0.drugs[d]} alarm began on day {s} (privately "
          f"{fac[node0.fac[i]]}, short of {short(i)}) cannot be singled out")


def state_id(s):
    if not re.fullmatch(r"S\d+", s):        # it becomes dataset names and SQL
        raise argparse.ArgumentTypeError(f"a state id like S2, not {s!r}")
    return s


def main():
    ap = argparse.ArgumentParser(description="The federation boundary on BigQuery (SYNTHETIC data).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("onboard", help="a state's private dataset and rows, its shared view, the national grant")
    o.add_argument("state", type=state_id)
    o.add_argument("--project", default=PROJECT, help="the state's own project, in production")
    o.add_argument("--national", default=NATIONAL, help="the national layer's service account")
    o.add_argument("--seed", type=int, default=5)
    p = sub.add_parser("proof", help="(a)-(e), querying as the national layer")
    p.add_argument("--day", type=int, default=120)
    p.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    if a.cmd == "onboard":
        onboard(a.state, a.seed, a.project, a.national)
    else:
        proof(a.day, a.seed)


if __name__ == "__main__":
    main()
