"""
Score scripts/moderation_bench.py output against hand labels, and price it.

    python scripts/moderation_score.py <bench out dir> <labels.json> [<more bench out dirs>...]

Labels are {item_id: {question: true | false | null}}; null is an image a
reviewer could not call either way, and is left out of that question's score.
Cost per image is the measured mean tokens times the published price
(2026-10-15), or the per-image price for the fixed-taxonomy APIs.
"""

import csv
import json
import os
import sys

QUESTIONS = ["cannabis", "smoking", "adult", "health_claim", "overlay"]

# USD per 1M tokens (in, out), or per image. Checked 2026-10-15 on the
# providers' pricing pages; standard tier, no batch discount.
PRICES = {
    "jev-1.14.0": {"in": 0.042, "out": 0.0},
    "gpt-6-luna": {"in": 0.10, "out": 0.50},
    "gpt-5.4-nano": {"in": 0.20, "out": 1.25},
    "gpt-5.4-mini": {"in": 0.75, "out": 4.50},
    "gpt-5.5": {"in": 5.00, "out": 30.00},
    "rekognition": {"image": 0.001},
    "omni-moderation": {"image": 0.0},
}


def main():
    dirs, labels_path = sys.argv[1:2] + sys.argv[3:], sys.argv[2]
    labels = json.load(open(labels_path))
    calls, summary = [], {}
    for d in dirs:
        calls += list(csv.DictReader(open(os.path.join(d, "calls.csv"))))
        summary.update(json.load(open(os.path.join(d, "summary.json"))))

    by_engine = {}
    for c in calls:
        by_engine.setdefault(c["engine"], []).append(c)

    print(f"{'engine':16} {'p50':>6} {'p95':>6} {'img/s x8':>8} {'$/image':>9} {'$/1M img':>9}  "
          + "  ".join(f"{q:>14}" for q in QUESTIONS) + "   false alarms")
    for eng, cs in by_engine.items():
        s, p = summary[eng], PRICES.get(eng, {})
        ok = [c for c in cs if not c["error"]]
        if "image" in p:
            per = p["image"]
        else:
            per = (s["in_tokens"] * p.get("in", 0) + s["out_tokens"] * p.get("out", 0)) / 1e6
        cells, fa, neg = [], 0, 0
        for q in QUESTIONS:
            tp = fn = fp = tn = 0
            supported = False
            for c in ok:
                truth = labels[c["item_id"]][q]
                got = json.loads(c["answers"]).get(q)
                if got is None or truth is None:
                    continue
                supported = True
                if truth and got: tp += 1
                elif truth: fn += 1
                elif got: fp += 1
                else: tn += 1
            fa += fp
            neg += fp + tn
            cells.append("n/a" if not supported else f"{tp}/{tp + fn} caught,{fp}fp")
        rate = len(cs) / s["parallel_wall_s"] if s.get("parallel_wall_s") else 0
        print(f"{eng:16} {s['p50_ms']:6.0f} {s['p95_ms']:6.0f} {rate:8.1f} {per:9.6f} {per * 1e6:9.0f}  "
              + "  ".join(f"{x:>14}" for x in cells) + f"   {fa}/{neg} ({fa / max(1, neg):.1%})")


if __name__ == "__main__":
    main()
