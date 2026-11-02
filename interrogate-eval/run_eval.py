"""Run arms over a unit file: python run_eval.py dev|v4live. Locked sets refuse to run twice."""
import glob, json, sys, time
from pathlib import Path
H = Path(__file__).resolve().parent
sys.path.insert(0, str(H))
from interrogate import agent, agent_solo, single_llm, load_fact, client_for
from thesis_redteam.llm import ClaudeCLIProvider
name = sys.argv[1]
units = json.load(open(H / f"units-{name}.json"))
outp = H / f"results-{name}.jsonl"
done = {json.loads(l)["unit"] for l in outp.open()} if outp.exists() else set()
provider = ClaudeCLIProvider(timeout=240)
client = client_for(H / "cache")
for u in units:
    if u["unit"] in done:
        continue
    rep = json.load(open(u["report"])) if u.get("report") else json.load(open(sorted(glob.glob(u["new_dir"] + "/*shareholder_return*.json"))[-1]))
    fact = load_fact(rep, u["premise"])
    t = time.time(); row = {"unit": u["unit"], "ticker": u["ticker"], "truth": u.get("truth")}
    arms = [("B", lambda: single_llm(u, fact, provider)), ("C", lambda: agent(u, fact, rep, provider, client)), ("D", lambda: agent_solo(u, fact.get("check"), provider, client))]
    if u.get("repeat_d"):
        arms.append(("D2", lambda: agent_solo(u, fact.get("check"), provider, client)))
    for arm, fn in arms:
        try:
            row[arm] = fn()
        except Exception as exc:
            row[arm] = {"verdict": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:300]}
    row["seconds"] = round(time.time() - t)
    with outp.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(row["unit"], row["truth"], row["B"]["verdict"], row["C"]["verdict"], row["D"]["verdict"], row["seconds"], flush=True)
