"""Seeded sample of premise units the code newly judged FALSE in the 21-month replay, excluding earlier dev/test cases."""
import json, random
from pathlib import Path
H = Path(__file__).resolve().parent
REPLAY = H.parents[0] / "thesis-watch/replay-v3-shareholder_return.jsonl"
used = {u["accession"] for n in ("dev", "test") for u in json.load(open(H / f"units-{n}.json"))}
pool = []
for line in REPLAY.open():
    r = json.loads(line)
    if "events" not in r:
        continue
    det = {k: (v["status"] if v["status"] in ("TRUE", "FALSE") else "TRUE") for k, v in r["baseline"]["facts"].items()}
    prev = "2025-01-02"
    for e in r["events"]:
        flips = []
        for k, v in e["facts"].items():
            st = v["status"]
            if st in ("TRUE", "FALSE"):
                if det.get(k, "TRUE") != st and st == "FALSE":
                    flips.append(k)
                det[k] = st
        if flips and e["accession"] not in used:
            for k in flips:
                pool.append({"unit": f"N-{r['ticker']}-{e['asof']}-{k}", "ticker": r["ticker"], "cik": r["cik"], "old_asof": prev, "new_asof": e["asof"],
                             "form": e["form"], "accession": e["accession"], "premise": k})
        prev = e["asof"]
pool.sort(key=lambda u: u["unit"])
rng = random.Random(20261102)
rng.shuffle(pool)
seen, out = {}, []
for u in pool:  # at most one unit per company, so one filer cannot dominate
    if u["ticker"] not in seen:
        seen[u["ticker"]] = 1
        out.append(u)
    if len(out) == 30:
        break
(H / "units-new.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print("pool", len(pool), "sampled", len(out))
