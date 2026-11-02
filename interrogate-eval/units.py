"""Build evaluation units: premises the code newly judged FALSE, with the independent label. Labels stay out of agent input."""
import glob, json, sys
from pathlib import Path
AV = Path(__file__).resolve().parents[1] / "agent-value"
OUT = Path(__file__).resolve().parent
LABEL = {"F1": "f1", "F2": "f2", "F3": "f3"}


def report(run_dir):
    files = sorted(glob.glob(str(run_dir / "*shareholder_return*.json")))
    return json.load(open(files[-1])) if files else None


def build(prefix, labels_dir, cases_file):
    cases = json.load(open(AV / cases_file))
    cases = cases if isinstance(cases, list) else cases["cases"]
    units = []
    for c in cases:
        run = AV / f"{prefix}runs" / c["case_id"]
        old, new = report(run / "old_asof"), report(run / "new_asof")
        lab_path = AV / labels_dir / f"{c['case_id']}.json"
        if not (old and new and lab_path.exists()):
            continue
        lab = json.load(open(lab_path))
        ostat = {f["id"]: f["status"] for f in old.get("fact_claims", [])}
        for f in new.get("fact_claims", []):
            if f["status"] != "FALSE" or ostat.get(f["id"]) == "FALSE" or f["id"] not in LABEL:
                continue
            truth = lab.get(f"{LABEL[f['id']]}_true_new")
            if truth not in (True, False):
                continue
            units.append({"unit": f"{c['case_id']}-{f['id']}", "ticker": c["ticker"], "cik": c["cik"], "old_asof": c["old_asof"], "new_asof": c["new_asof"],
                          "form": c["form"], "accession": c["accession"], "premise": f["id"], "check": f.get("check"),
                          "truth": "ARTIFACT" if truth else "REAL", "new_dir": str(run / "new_asof"), "old_dir": str(run / "old_asof")})
    return units


if __name__ == "__main__":
    for name, prefix, labels, cases in [("dev", "", "labels", "CASES-FROZEN.json"), ("test", "TEST-", "test-labels", "TEST-CASES-FROZEN.json")]:
        u = build(prefix, labels, cases)
        (OUT / f"units-{name}.json").write_text(json.dumps(u, ensure_ascii=False, indent=1))
        print(name, "units", len(u), "REAL", sum(x["truth"] == "REAL" for x in u), "ARTIFACT", sum(x["truth"] == "ARTIFACT" for x in u))
