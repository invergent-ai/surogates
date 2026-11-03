"""Summarize lab runs: python3 report.py runs/X [runs/Y ...] [--vs base]"""
from __future__ import annotations

import collections
import json
import random
import statistics as st
import sys
from pathlib import Path


def load(dirs):
    rows = []
    for d in dirs:
        for f in Path(d).glob("*/*/s*/result.json"):
            r = json.loads(f.read_text())
            r["_run"] = str(d)
            rows.append(r)
    return rows


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--skip" in sys.argv:
        args = [a for a in args if a != sys.argv[sys.argv.index("--skip") + 1]]
    ref = None
    if "--vs" in sys.argv:
        ref = sys.argv[sys.argv.index("--vs") + 1]
        args = [a for a in args if a != ref]
    rows = load(args)
    if "--skip" in sys.argv:
        drop = set(sys.argv[sys.argv.index("--skip") + 1].split(","))
        rows = [r for r in rows if r["scenario"] not in drop]
    by_v = collections.defaultdict(list)
    for r in rows:
        by_v[r["variant"]].append(r)
    print(f"{'variant':12} {'n':>4} {'score':>6} {'tok1':>7} {'ptok':>8} {'ctok':>7} {'iters':>5} {'wall':>6} {'skill%':>6} {'ask%':>5} {'bad':>4} {'cap':>4}")
    for v, rs in sorted(by_v.items()):
        print(f"{v:12} {len(rs):4} {st.mean(r['score'] for r in rs):6.3f} {st.mean((r['prompt_tokens_first'] or 0) for r in rs):7.0f} "
              f"{st.mean(r['prompt_tokens_total'] for r in rs):8.0f} {st.mean(r['completion_tokens'] for r in rs):7.0f} "
              f"{st.mean(r['iters'] for r in rs):5.1f} {st.mean(r['wall'] for r in rs):6.1f} "
              f"{100*st.mean(1 if r['skill_loads'] else 0 for r in rs):6.0f} {100*st.mean(1 if (r['questions'] or r['autoreplies']) else 0 for r in rs):5.0f} "
              f"{sum(r['bad_calls']+r['unknown_tools'] for r in rs):4} {sum(1 for r in rs if r['hit_cap']):4}")
    scen = sorted({r["scenario"] for r in rows})
    vs = sorted(by_v)
    print()
    print(f"{'scenario':22} " + " ".join(f"{v[:10]:>10}" for v in vs) + "   skills / notes(first variant)")
    for s in scen:
        cells = []
        for v in vs:
            xs = [r["score"] for r in by_v[v] if r["scenario"] == s]
            cells.append(f"{st.mean(xs):10.2f}" if xs else f"{'-':>10}")
        ex = [r for r in by_v[vs[0]] if r["scenario"] == s]
        sk = collections.Counter(x for r in ex for x in r["skill_loads"])
        print(f"{s:22} " + " ".join(cells) + "   " + ",".join(f"{k}x{n}" for k, n in sk.items()))
    if ref and ref in by_v:
        print()
        for v in vs:
            if v == ref:
                continue
            a = {(r["scenario"], r["sample"]): r["score"] for r in by_v[v]}
            b = {(r["scenario"], r["sample"]): r["score"] for r in by_v[ref]}
            # paired by scenario: mean over samples per scenario, then bootstrap over scenarios
            sa = collections.defaultdict(list); sb = collections.defaultdict(list)
            for (s, k), x in a.items():
                sa[s].append(x)
            for (s, k), x in b.items():
                sb[s].append(x)
            common = sorted(set(sa) & set(sb))
            d = [st.mean(sa[s]) - st.mean(sb[s]) for s in common]
            rng = random.Random(0)
            boots = sorted(st.mean(rng.choice(d) for _ in d) for _ in range(4000))
            print(f"{v} - {ref}: diff {st.mean(d):+.3f}  95% CI [{boots[100]:+.3f}, {boots[3899]:+.3f}] over {len(common)} scenarios")


if __name__ == "__main__":
    main()
