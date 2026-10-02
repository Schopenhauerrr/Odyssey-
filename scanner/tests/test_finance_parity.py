"""The app (JS) and the scanner (Python) must compute identical numbers.
Needs node.js:  python scanner/tests/test_finance_parity.py"""
import itertools, json, re, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scanner"))
from finance import compute_deal  # noqa: E402

s = json.loads((ROOT / "docs/data/settings.json").read_text())
src = (ROOT / "app/odyssey.html").read_text()
js_fn = re.search(r"function computeDeal\(.*?\n}\n", src, re.S).group(0)

cases = []
for resale, asking, plat, cond, ship, extra, learned in itertools.product(
        [600, 1800, 30000, 0], [0, 60, 1000, 7500], list(s["platforms"]), list(s["conditions"]),
        [False, True], [0, 111.9], [None, 9]):
    cases.append(dict(resale=resale, days=14, asking=asking, platform=plat, condition=cond, ship=ship, extra=extra, learned=learned))

js = js_fn + f"""
const s = {json.dumps(s)}; const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(c => computeDeal(c.resale, c.days, c.asking, s,
  {{platform: c.platform, condition: c.condition, ship: c.ship, extra: c.extra, learnedDays: c.learned}}))));"""
out = json.loads(subprocess.run(["node", "-"], input=js, capture_output=True, text=True, check=True).stdout)

bad = 0
for c, j in zip(cases, out):
    p = compute_deal(c["resale"], c["days"], c["asking"], s, platform=c["platform"], condition=c["condition"],
                     ship=c["ship"], extra_cost=c["extra"], learned_days=c["learned"])
    for k, v in p.items():
        same = v == j[k] if isinstance(v, str) else abs(v - j[k]) < 1e-6
        if not same:
            bad += 1
            if bad < 5:
                print("MISMATCH", c, k, v, j[k])
print(f"{len(cases)} cases, {bad} mismatches")
assert bad == 0
