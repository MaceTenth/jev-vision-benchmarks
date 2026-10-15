"""
Audit catalogue photos against their titles with Jev 1.14: is the photo the
right kind of product, does its label name a different variant, and does the
title promise more units than the photo shows?

    python scripts/jev_photo_audit.py --catalog products.csv [--sample 200] [--seed 1] [--all]

--catalog is a CSV file or URL with a `title` and an `image_url` column
(`item_id` and `product_url` are carried through when present). Writes every
answer to <out>/audit.csv plus a contact sheet of flagged photos.
Before the audit it re-runs four listings whose answer we know, so a change in
model behaviour shows up as a failed control rather than a quiet shift in flags.

Images are Danger Zone in TypeSafe's terms: opt-in by header, not vouched for
in production. Flags are for a person to review, not for automatic edits.
"""

import argparse
import csv
import io
import os
import random
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image as PILImage, ImageDraw
from typesafe_sdk import Image, Noul, RetryPolicy, TypeSafeClient, TypeSafeError

MODEL = "jev-1.14.0"  # pinned: images are 1.14 territory, and jev-latest is about to move
IMAGES_ON = {"TypeSafe-Danger-Zone-Images": "true"}
PRICE_PER_MTOK = 0.042
FLAG_AT = 0.5
# The variant question over-fires on labels that only name a brand or a blend
# name. On the 2026-10-15 sample of 200, --variant-cut 0.85 halved false alarms
# (23 -> 10) and still kept all 19 real problems, but the known sage-candle
# mismatch scores 0.87, so that cut leaves almost no margin. A false alarm costs
# a reviewer seconds; a miss leaves a wrong listing live. Default stays at 0.5.
VARIANT_FLAG_AT = FLAG_AT

# For `kind` yes is good; for the other two yes is the problem.
QUESTIONS = {
    "kind": "Does `photo` show the type of product that `title` describes?",
    "variant": "Does text or packaging in `photo` name a different flavour, scent, colour or "
               "variant than the one in `title`?",
    "count": "Does `title` promise more units than `photo` shows, such as a bundle, multipack "
             "or variety pack pictured as a single item?",
}

# Listings checked by eye on 2026-10-15: (cdn image id, title, expected flags).
CONTROLS = [
    ("gastaktjb3yxqa89tqnnaqkt6nju", "Single Origin Papua New Guinea Coffee Beans, Toffee & Cherry "
     "Citrus Notes, Air-Roasted Fresh, 1 lb Bag, Whole Bean or Ground", set()),
    ("pqivexa3msgx3i4pjynxhucko50j", "Date-Sweetened Hot Sauce, Sweet Heat Five Pepper Blend, "
     "Plant-Based, 8 fl oz", set()),
    ("odfjjkie4k706tfkaskgui4gflxm", "Scented Candle Tin, Coconut Wax Blend, Coastal Sage & Sea Salt, "
     "6 oz Travel Tin, Herbal Fresh Home Fragrance", {"variant"}),
    ("akrq71wg55uuhb7a4nwsy1adlk1b", "Keto Cookie Baking Mix Variety Pack, Sugar-Free & Grain-Free, "
     "5-Box Bundle (Choc Chip, PB, Sugar, Macaroon, Pistachio Shortbread)", {"count"}),
]


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def prepare(url, out):
    """JPEG within Jev's limits (each side 256-1568 px), or None. Catalogue
    covers can be WebP, which Jev does not take."""
    path = os.path.join(out, "img", url.rstrip("/").rsplit("/", 1)[-1] + ".jpg")
    if os.path.exists(path):
        return path
    try:
        img = PILImage.open(io.BytesIO(fetch(url))).convert("RGB")
    except Exception:
        return None
    if max(img.size) > 1568:
        img.thumbnail((1568, 1568))
    if min(img.size) < 256:
        k = 256 / min(img.size)
        img = img.resize((round(img.width * k), round(img.height * k)))
    if max(img.size) > 1568:
        return None  # too elongated to fit both limits
    img.save(path, quality=92)
    return path


def flags(p, variant_cut=VARIANT_FLAG_AT):
    out = set()
    if p["kind"] < FLAG_AT:
        out.add("kind")
    if p["variant"] >= variant_cut:
        out.add("variant")
    if p["count"] >= FLAG_AT:
        out.add("count")
    return out


def ask(client, path, title):
    r = client.system_one(model=MODEL, state={"photo": Image(Path(path)), "title": title},
                          questions={k: Noul(instructions=q) for k, q in QUESTIONS.items()})
    return {k: r.nouls[k].noul for k in QUESTIONS}, r.usage.input_tokens


def sheet(rows, out, name, per_row=4, size=300):
    """Flagged photos with their title and reason underneath, for review."""
    if not rows:
        return None
    cap = 92
    w, h = per_row * size, ((len(rows) + per_row - 1) // per_row) * (size + cap)
    canvas = PILImage.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(canvas)
    for i, r in enumerate(rows):
        x, y = (i % per_row) * size, (i // per_row) * (size + cap)
        canvas.paste(PILImage.open(r["_path"]).convert("RGB").resize((size, size)), (x, y))
        words, lines, line = r["title"].split(), [], ""
        for wd in words:
            if len(line) + len(wd) > 46:
                lines.append(line)
                line = ""
            line += wd + " "
        lines = (lines + [line])[:4]
        reason = f"#{i + 1} {r['flags']}  kind {float(r['p_kind']):.2f} var {float(r['p_variant']):.2f} " \
                 f"cnt {float(r['p_count']):.2f}"
        draw.text((x + 4, y + size + 2), reason, fill="red")
        for j, ln in enumerate(lines):
            draw.text((x + 4, y + size + 18 + j * 17), ln.strip(), fill="black")
    path = os.path.join(out, name)
    canvas.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True, help="CSV path or URL with title and image_url columns")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--all", action="store_true", help="audit every product, not a sample")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--variant-cut", type=float, default=VARIANT_FLAG_AT,
                    help="p(yes) at which the variant question flags; 0.85 trades recall for fewer alarms")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "jev_photo_audit"))
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "img"), exist_ok=True)

    load_dotenv()
    key = os.environ.get("TYPESAFE_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        sys.exit("TYPESAFE_KEY is not set")
    client = TypeSafeClient(api_key=key, headers=IMAGES_ON, timeout=120.0,
                            retry=RetryPolicy(max_retries=4))

    print("Controls (known answers):")
    paths = [prepare(f"https://cdn.loyaltie.com/{c[0]}", args.out) for c in CONTROLS]
    ok = 0
    for (_, title, expected), path in zip(CONTROLS, paths):
        p, _ = ask(client, path, title)
        got = flags(p, args.variant_cut)
        ok += got == expected
        print(f"  {'✓' if got == expected else '✗'} expected {sorted(expected) or 'clean'}, "
              f"got {sorted(got) or 'clean'}  " + " ".join(f"{k} {v:.2f}" for k, v in p.items())
              + f"  {title[:50]}")
    if ok < len(CONTROLS):
        print("  A control failed: model behaviour has shifted, read the flags below with care.")

    raw = fetch(args.catalog, 120) if args.catalog.startswith("http") else open(args.catalog, "rb").read()
    rows = [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8")))
            if r.get("image_url", "").startswith("http")]
    catalogue = len(rows)
    if not args.all:
        rows = random.Random(args.seed).sample(rows, min(args.sample, len(rows)))

    t0 = time.time()
    with ThreadPoolExecutor(16) as pool:
        for r, path in zip(rows, pool.map(lambda r: prepare(r["image_url"], args.out), rows)):
            r["_path"] = path

    def audit(r):
        if not r["_path"]:
            return {**r, "error": "image unusable"}, 0
        try:
            p, toks = ask(client, r["_path"], r["title"])
        except TypeSafeError as e:
            return {**r, "error": str(e)[:200]}, 0
        return {**r, **{f"p_{k}": f"{v:.3f}" for k, v in p.items()},
                "flags": ",".join(sorted(flags(p, args.variant_cut)))}, toks

    with ThreadPoolExecutor(args.workers) as pool:
        done = list(pool.map(audit, rows))
    secs = time.time() - t0
    results = [d for d, _ in done]
    tokens = sum(t for _, t in done)

    fields = ["item_id", "title", "product_url", "image_url", "p_kind", "p_variant", "p_count",
              "flags", "error"]
    with open(os.path.join(args.out, "audit.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    answered = [r for r in results if not r.get("error")]
    flagged = [r for r in answered if r["flags"]]
    print(f"\nAudited {len(answered)} of {len(rows)} sampled products "
          f"({len(rows) - len(answered)} skipped: unusable image or API error)")
    for k in QUESTIONS:
        n = sum(1 for r in flagged if k in r["flags"].split(","))
        print(f"  {k:8} {n:4} flagged")
    print(f"  any      {len(flagged):4} flagged ({len(flagged) / max(1, len(answered)):.1%})")

    per = tokens / max(1, len(answered))
    print(f"\nCost: {tokens:,} input tokens = ${tokens / 1e6 * PRICE_PER_MTOK:.3f} "
          f"({per:,.0f} tokens per product); {secs:.0f}s with {args.workers} workers")
    if not args.all:
        print(f"Whole catalogue ({catalogue:,} products): about "
              f"${catalogue * per / 1e6 * PRICE_PER_MTOK:.2f} and "
              f"{catalogue * secs / max(1, len(rows)) / 60:.0f} min at this concurrency")

    s = sheet(flagged, args.out, "flagged.png")
    print(f"\nAll answers: {os.path.join(args.out, 'audit.csv')}")
    if s:
        print(f"Flagged photos with titles: {s}")


if __name__ == "__main__":
    main()
