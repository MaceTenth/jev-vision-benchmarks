"""
Image moderation on our own product photos: dedicated moderation APIs vs
general vision LLMs vs Jev, measured on the same images for latency,
throughput, tokens and cost.

    pip install -r requirements.txt
    python scripts/moderation_bench.py [--images data/moderation/images.csv] [--limit N]

Every engine sees the same JPEGs. The ones that take custom questions (Jev, the
GPT models) answer the same five; the fixed-taxonomy APIs answer what their
categories cover, mapped onto the same five where a mapping exists.

The images are listed in data/moderation/images.csv: 15 picked because they
were likely to trip a policy, then 45 drawn at random from the catalog.

Two passes per engine: one request at a time (latency), then all images with
--workers in parallel (throughput). Writes every call to <out>/calls.csv and the
per-engine numbers to <out>/summary.json; scripts/moderation_score.py prices
and scores them.
"""

import argparse
import base64
import csv
import io
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image as PILImage

QUESTIONS = {
    "cannabis": "Does the image show cannabis (a cannabis leaf or plant) or CBD/THC branding?",
    "smoking": "Does the image show smoking or vaping products, such as joints, cigarettes, pipes or vapes?",
    "adult": "Does the image show nudity or sexually suggestive content?",
    "health_claim": "Does text in the image claim the product cures, treats or prevents a disease or medical condition?",
    "overlay": "Has promotional text or a badge (sale, discount, free shipping, best seller) been added on top of "
               "the photo, apart from the product's own packaging?",
}

SYSTEM = ("You review product photos for an online marketplace. Answer each question about the image "
          "with true or false. Judge only what is visible in the image.")


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def prepare(url, out):
    path = os.path.join(out, "img", url.rstrip("/").rsplit("/", 1)[-1] + ".jpg")
    if not os.path.exists(path):
        img = PILImage.open(io.BytesIO(fetch(url))).convert("RGB")
        if max(img.size) > 1568:
            img.thumbnail((1568, 1568))
        if min(img.size) < 256:
            k = 256 / min(img.size)
            img = img.resize((round(img.width * k), round(img.height * k)))
        img.save(path, quality=92)
    return path


# ── engines: each returns (answers {q: bool|None}, input_tokens, output_tokens, raw) ──

def jev_engine(key):
    from typesafe_sdk import Image, Noul, RetryPolicy, TypeSafeClient
    client = TypeSafeClient(api_key=key, headers={"TypeSafe-Danger-Zone-Images": "true"}, timeout=120.0,
                            retry=RetryPolicy(max_retries=3))
    qs = {k: Noul(instructions=q.replace("the image", "`photo`")) for k, q in QUESTIONS.items()}

    def run(path):
        r = client.system_one(model="jev-1.14.0", state={"photo": Image(Path(path))}, questions=qs)
        p = {k: r.nouls[k].noul for k in QUESTIONS}
        return {k: v >= 0.5 for k, v in p.items()}, r.usage.input_tokens, r.usage.output_tokens, p
    return run


def gpt_engine(model):
    from openai import OpenAI
    client = OpenAI(max_retries=3, timeout=120)
    schema = {"type": "object", "additionalProperties": False, "required": list(QUESTIONS),
              "properties": {k: {"type": "boolean"} for k in QUESTIONS}}
    text = "\n".join(f"{k}: {q}" for k, q in QUESTIONS.items())
    effort = {}

    def call(content, eff):
        kw = {"reasoning_effort": eff} if eff else {}
        return client.chat.completions.create(
            model=model, messages=[{"role": "system", "content": SYSTEM},
                                   {"role": "user", "content": content}],
            response_format={"type": "json_schema", "json_schema": {"name": "moderation", "strict": True,
                                                                    "schema": schema}},
            max_completion_tokens=4000, **kw)

    def run(path):
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                   {"type": "text", "text": text}]
        if "eff" not in effort:  # the lowest reasoning effort this model accepts
            for eff in ("none", "minimal", "low", None):
                try:
                    r = call(content, eff)
                    effort["eff"] = eff
                    break
                except Exception as e:  # noqa: BLE001 - probing accepted values
                    if "reasoning" not in str(e).lower() and "effort" not in str(e).lower():
                        raise
        else:
            r = call(content, effort["eff"])
        a = json.loads(r.choices[0].message.content)
        u = r.usage
        return a, u.prompt_tokens, u.completion_tokens, {"effort": effort.get("eff")}
    run.effort = effort
    return run


def rekognition_engine():
    import boto3
    client = boto3.client("rekognition", region_name="us-east-1")

    def run(path):
        r = client.detect_moderation_labels(Image={"Bytes": open(path, "rb").read()}, MinConfidence=50)
        labels = [(l["Name"], l.get("ParentName", ""), round(l["Confidence"], 1)) for l in r["ModerationLabels"]]
        names = " ".join(f"{n} {p}" for n, p, _ in labels).lower()
        a = {"cannabis": None, "smoking": ("tobacco" in names or "smok" in names or "drug" in names) or False,
             "adult": any(w in names for w in ("nudity", "explicit", "suggestive", "swimwear", "underwear")),
             "health_claim": None, "overlay": None}
        return a, 0, 0, labels
    return run


def omni_engine():
    from openai import OpenAI
    client = OpenAI(max_retries=3, timeout=120)

    def run(path):
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        r = client.moderations.create(model="omni-moderation-latest",
                                      input=[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}])
        res = r.results[0]
        cats = {k: v for k, v in res.categories.model_dump().items() if v}
        a = {"cannabis": None, "smoking": None, "adult": bool(res.categories.sexual), "health_claim": None,
             "overlay": None}
        return a, 0, 0, cats
    return run


def timed(run, path):
    t = time.perf_counter()
    try:
        a, i, o, raw = run(path)
        err = ""
    except Exception as e:  # noqa: BLE001 - a failed call is a result here
        a, i, o, raw, err = {}, 0, 0, None, f"{type(e).__name__}: {str(e)[:160]}"
    return {"ms": (time.perf_counter() - t) * 1000, "answers": a, "in": i, "out": o, "raw": raw, "error": err}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                     "data", "moderation", "images.csv"))
    ap.add_argument("--limit", type=int, default=0, help="first N images only, for a quick check")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--engines", default="jev-1.14.0,gpt-5.4-nano,gpt-5.4-mini,rekognition,omni-moderation")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "moderation_bench"))
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "img"), exist_ok=True)
    load_dotenv()

    picked = list(csv.DictReader(open(args.images, newline="", encoding="utf-8")))
    if args.limit:
        picked = picked[:args.limit]
    with ThreadPoolExecutor(16) as pool:
        paths = list(pool.map(lambda r: prepare(r["image_url"], args.out), picked))
    print(f"{len(paths)} images from {args.images}")

    key = os.environ.get("TYPESAFE_KEY") or os.environ.get("TYPESAFE_API_KEY")
    makers = {"jev-1.14.0": lambda: jev_engine(key), "rekognition": rekognition_engine,
              "omni-moderation": omni_engine}
    calls, summary = [], {}
    for name in args.engines.split(","):
        run = makers[name]() if name in makers else gpt_engine(name)
        timed(run, paths[0])  # warm-up: connection setup and, for GPT, the effort probe
        seq = [timed(run, p) for p in paths]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(args.workers) as pool:
            par = list(pool.map(lambda p: timed(run, p), paths))
        wall = time.perf_counter() - t0
        ok = [c for c in seq if not c["error"]]
        lat = [c["ms"] for c in ok]
        summary[name] = {
            "ok": len(ok), "n": len(seq),
            "p50_ms": statistics.median(lat) if lat else None, "p95_ms": pct(lat, 95) if lat else None,
            "parallel_wall_s": wall, "parallel_errors": sum(1 for c in par if c["error"]),
            "in_tokens": statistics.mean(c["in"] for c in ok) if ok else 0,
            "out_tokens": statistics.mean(c["out"] for c in ok) if ok else 0,
            "effort": getattr(run, "effort", {}).get("eff", "") if hasattr(run, "effort") else "",
        }
        for r, p, c in zip(picked, paths, seq):
            calls.append({"engine": name, "item_id": r["item_id"], "title": r["title"][:120],
                          "image": os.path.basename(p), "ms": round(c["ms"]), "in": c["in"], "out": c["out"],
                          "answers": json.dumps(c["answers"]), "raw": json.dumps(c["raw"], default=str)[:600],
                          "error": c["error"]})
        s = summary[name]
        print(f"  {name:16} ok {s['ok']}/{s['n']}  p50 {s['p50_ms'] or 0:6.0f} ms  p95 {s['p95_ms'] or 0:6.0f} ms  "
              f"{len(paths)} images x{args.workers} in {wall:5.1f}s  tokens in {s['in_tokens']:.0f} "
              f"out {s['out_tokens']:.0f}  {s['effort'] or ''}")
        errs = [c["error"] for c in seq + par if c["error"]]
        if errs:
            print(f"    errors: {len(errs)}, e.g. {errs[0]}")

    with open(os.path.join(args.out, "calls.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(calls[0]))
        w.writeheader()
        w.writerows(calls)
    json.dump(summary, open(os.path.join(args.out, "summary.json"), "w"), indent=1)
    print(f"\nPer-call results: {os.path.join(args.out, 'calls.csv')}")


if __name__ == "__main__":
    main()
