"""Accuracy check for the optimizer: random scenarios against an independent brute-force oracle.

  python scripts/accuracy_check.py --local 3000                       # in-process, no network
  python scripts/accuracy_check.py --local 0 --live 150 --api-url URL --origin ORIGIN   # the deployed API

For each random scenario it checks (1) status and total effort equal the oracle's exhaustive search and (2) the returned
numbers are internally consistent (baseline, production, coordination, effort, net savings, totals, capacity, nobody worse
off, every deliverable assigned once to a provider that supports the service, minutes equal that provider's estimate).
The oracle here is written separately from optimizer/engine.py. It is a test, not a proof.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
import urllib.error
import urllib.request

SERVICES = ["coding", "design", "writing"]


def make_scenario(rng: random.Random, n: int) -> dict:
    businesses, deliverables = [], []
    for i in range(n):
        svcs = rng.sample(SERVICES, rng.choice([1, 2, 2, 3]))
        est = [{"service": s, "minutes": rng.choice(range(30, 601, 30))} for s in svcs]
        businesses.append(
            {
                "id": f"biz{i}",
                "name": f"Biz {i}",
                "capacityMinutes": rng.choice(range(240, 1441, 30)),
                "coordinationMinutes": rng.choice([0, 0, 15, 30, 60, 120]),
                "estimates": est,
            }
        )
        slow = max(est, key=lambda e: e["minutes"])["service"]
        deliverables.append({"id": f"d{i}", "ownerId": f"biz{i}", "service": slow if rng.random() < 0.7 else rng.choice(svcs)})
    return {"schemaVersion": "1", "businesses": businesses, "deliverables": deliverables, "confirmed": True}


def oracle(sc: dict):
    est = {b["id"]: {e["service"]: e["minutes"] for e in b["estimates"]} for b in sc["businesses"]}
    cap = {b["id"]: b["capacityMinutes"] for b in sc["businesses"]}
    coord = {b["id"]: b["coordinationMinutes"] for b in sc["businesses"]}
    ids = [b["id"] for b in sc["businesses"]]
    base = {i: 0 for i in ids}
    for d in sc["deliverables"]:
        base[d["ownerId"]] += est[d["ownerId"]][d["service"]]
    options = [[b for b in ids if d["service"] in est[b]] for d in sc["deliverables"]]
    feasible_any, best = False, None
    for combo in itertools.product(*options):
        prod = {i: 0 for i in ids}
        involved = set()
        cross = False
        for d, p in zip(sc["deliverables"], combo):
            prod[p] += est[p][d["service"]]
            if p != d["ownerId"]:
                cross = True
                involved.update([p, d["ownerId"]])
        eff = {i: prod[i] + (coord[i] if i in involved else 0) for i in ids}
        if any(eff[i] > cap[i] or eff[i] > base[i] for i in ids):
            continue
        feasible_any = True
        if cross and sum(base.values()) - sum(eff.values()) > 0:
            tot = sum(eff.values())
            if best is None or tot < best:
                best = tot
    if best is not None:
        return "collaboration", best
    # no beneficial exchange: feasible if every owner can do its own work within capacity
    own_ok = all(base[i] <= cap[i] for i in ids)
    return ("no_exchange" if own_ok else "infeasible"), None


def check_result(sc: dict, res: dict) -> list[str]:
    bad: list[str] = []
    est = {b["id"]: {e["service"]: e["minutes"] for e in b["estimates"]} for b in sc["businesses"]}
    cap = {b["id"]: b["capacityMinutes"] for b in sc["businesses"]}
    coord = {b["id"]: b["coordinationMinutes"] for b in sc["businesses"]}
    owner = {d["id"]: d for d in sc["deliverables"]}
    if res["status"] == "infeasible":
        if res["assignments"] or res["businessMetrics"] is not None or res["totals"] is not None or res["certificate"] is not None:
            bad.append("infeasible result carries data")
        return bad
    got = {a["deliverableId"]: a for a in res["assignments"]}
    if set(got) != set(owner):
        bad.append("assignments do not cover every deliverable exactly once")
        return bad
    if len(res["assignments"]) != len(got):
        bad.append("duplicate assignment")
    prod = {i: 0 for i in est}
    involved = set()
    for did, a in got.items():
        svc = owner[did]["service"]
        if svc not in est[a["providerId"]]:
            bad.append(f"{did}: provider cannot do {svc}")
            continue
        if a["minutes"] != est[a["providerId"]][svc]:
            bad.append(f"{did}: minutes differ from provider estimate")
        prod[a["providerId"]] += est[a["providerId"]][svc]
        if a["providerId"] != owner[did]["ownerId"]:
            involved.update([a["providerId"], owner[did]["ownerId"]])
    base = {i: 0 for i in est}
    for d in sc["deliverables"]:
        base[d["ownerId"]] += est[d["ownerId"]][d["service"]]
    metrics = {m["businessId"]: m for m in res["businessMetrics"]}
    for i in est:
        m = metrics[i]
        c = coord[i] if i in involved else 0
        exp = dict(baselineMinutes=base[i], productionMinutes=prod[i], coordinationMinutes=c,
                   effortMinutes=prod[i] + c, netSavingsMinutes=base[i] - prod[i] - c)
        for k, v in exp.items():
            if m[k] != v:
                bad.append(f"{i}.{k}: got {m[k]} expected {v}")
        if m["effortMinutes"] > cap[i]:
            bad.append(f"{i} over capacity")
        if m["netSavingsMinutes"] < 0:
            bad.append(f"{i} worse off")
    t = res["totals"]
    sums = dict(
        baselineMinutes=sum(base.values()), productionMinutes=sum(prod.values()),
        effortMinutes=sum(m["effortMinutes"] for m in metrics.values()),
        netSavingsMinutes=sum(m["netSavingsMinutes"] for m in metrics.values()),
    )
    for k, v in sums.items():
        if t[k] != v:
            bad.append(f"totals.{k}: got {t[k]} expected {v}")
    if t["grossSavingsMinutes"] != t["baselineMinutes"] - t["productionMinutes"]:
        bad.append("totals.grossSavings wrong")
    if res["certificate"] is None or res["certificate"].get("verification") != "valid":
        bad.append("certificate missing or not valid")
    return bad


def run_case(sc: dict, res: dict) -> list[str]:
    status, total = oracle(sc)
    bad = []
    if res["status"] != status:
        bad.append(f"status {res['status']} vs oracle {status}")
    elif status == "collaboration" and res["totals"]["effortMinutes"] != total:
        bad.append(f"effort {res['totals']['effortMinutes']} vs oracle optimum {total}")
    return bad + check_result(sc, res)


def post(url: str, origin: str, scenario: dict) -> dict:
    body = json.dumps({"scenario": scenario}).encode()
    for attempt in range(8):
        req = urllib.request.Request(url + "/optimize", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Origin": origin})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["result"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "5")) + 1)
                continue
            raise
    raise RuntimeError("rate limited too long")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", type=int, default=0)
    ap.add_argument("--live", type=int, default=0)
    ap.add_argument("--api-url")
    ap.add_argument("--origin")
    ap.add_argument("--seed", type=int, default=20261107)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    failures, counts = [], {}

    if a.local:
        from optimizer.api import optimize
        for k in range(a.local):
            sc = make_scenario(rng, rng.choice([3, 3, 3, 4, 5]) if k % 3 else 3)
            try:
                res = optimize(sc, extended=len(sc["businesses"]) > 3)
            except Exception as e:  # noqa: BLE001
                failures.append((k, "local", repr(e)[:150]))
                continue
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            bad = run_case(sc, res)
            if bad:
                failures.append((k, "local", bad[:3]))
        print(f"local: {a.local} cases, statuses {counts}, failures {len([f for f in failures if f[1]=='local'])}")

    if a.live:
        rng = random.Random(a.seed + 1)
        lc = {}
        for k in range(a.live):
            sc = make_scenario(rng, 3)
            try:
                res = post(a.api_url, a.origin, sc)
            except Exception as e:  # noqa: BLE001
                failures.append((k, "live", repr(e)[:150]))
                continue
            lc[res["status"]] = lc.get(res["status"], 0) + 1
            bad = run_case(sc, res)
            if bad:
                failures.append((k, "live", bad[:3]))
            time.sleep(0.4)
        print(f"live: {a.live} cases, statuses {lc}, failures {len([f for f in failures if f[1]=='live'])}")

    for f in failures[:15]:
        print("FAIL", f)
    print("RESULT", "PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
