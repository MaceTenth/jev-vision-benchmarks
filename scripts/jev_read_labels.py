"""
How well does Jev read the text printed on a product photo? Compared with
Tesseract OCR, the pre-LLM way, on the same photos.

    pip install -r requirements.txt
    python scripts/jev_read_labels.py

Needs the `tesseract` binary (brew install tesseract) for the OCR column.

Jev never returns free text, only yes/no, a pick among options, a score or a
box. So it cannot transcribe a label; it can only pick which of the given
readings is printed. Each question has the true reading and three near-misses,
and Jev sees the photo alone (no title). OCR transcribes, and its pick is the
option closest to anything it read.

Text sizes:
  large   brand name
  medium  product, flavour or scent name
  small   net weight or volume
  tiny    the "distributed by" line

Then the photo-vs-title case: Jev gets the photo AND the title and is asked
about something the title claims but the label does not say (three coffee bags
have a blank Origin and Net Weight; the sage candle's label names a different
scent). This tests reading the label rather than repeating the title.
"""

import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image as PILImage
from typesafe_sdk import Choice, Image, Noul, RetryPolicy, TypeSafeClient

MODELS = ["jev-1.14.0", "jev-1.13.0"]
IMAGES_ON = {"TypeSafe-Danger-Zone-Images": "true"}
CDN = "https://cdn.loyaltie.com/"

QUESTIONS = {
    "brand": ("large", "What brand name is printed on the packaging in `photo`?"),
    "name": ("medium", "What product, flavour or scent name is printed on the label in `photo`?"),
    "size": ("small", "What net weight or volume is printed on the label in `photo`?"),
    "tiny": ("tiny", "Which company is named in the small 'distributed by' line on the label in `photo`?"),
}

ONLY_LABEL = "Answer from the text printed on the label in `photo` only, not from `title`: "
CHECKS = {
    "origin": ONLY_LABEL + "does the label name Papua New Guinea?",
    "weight": ONLY_LABEL + "is a net weight value filled in on the label?",
    "coastal": ONLY_LABEL + "does the label name the scent Coastal Sage & Sea Salt?",
    "california": ONLY_LABEL + "does the label name the scent California Sage?",
}

# Truth first, then near-misses. Some near-misses are what a model would guess
# without reading: 237 mL and 453 g are the textbook conversions of 8 fl oz and
# 1 lb, while these labels print 236 mL and 455 g.
DISTRIBUTOR = ("Temecula Coffee Roasters",
               ["Ventura Coffee Roasters", "Temple Coffee Roasters", "Tacoma Coffee Roasters"])
PRESENT_DAY = ("Present Day", ["Present Way", "Pleasant Day", "Presently"])
PHOTOS = {
    "pops coffee": dict(
        image="gastaktjb3yxqa89tqnnaqkt6nju",
        title="Single Origin Papua New Guinea Coffee Beans, Toffee & Cherry Citrus Notes, "
              "Air-Roasted Fresh, 1 lb Bag, Whole Bean or Ground",
        read={"brand": ("Pops Coffee Company",
                        ["Pop's Roasting Co.", "Papa's Coffee Company", "Pike Coffee Company"]),
              "name": ("Papua New Guinea", ["Panama Geisha", "New Guinea Peaberry", "Papua Highlands"]),
              "size": ("1 lb / 16 oz / 455 g",
                       ["1 lb / 16 oz / 453 g", "12 oz / 340 g", "2 lb / 32 oz / 907 g"])},
        checks={"origin": True, "weight": True}),
    "lilie's coffee": dict(
        image="fyx0ert8z10876aoeo5we3m79i9r",
        title="Single Origin Papua New Guinea Coffee, Bourbon & Typica, Medium Roast, "
              "Caramel Honey Fruit Notes, Washed, 12 oz",
        read={"brand": ("Lilie's Coffee", ["Lily's Coffee", "Lillie's Café", "Leila's Coffee"]),
              "tiny": DISTRIBUTOR},
        checks={"origin": False, "weight": False}),
    "arturos coffee": dict(
        image="9juknitg4cgtjvujwf6nhnompykr",
        title="Single Origin Papua New Guinea Coffee, Medium Roast, Caramel Honey Fruit Notes, "
              "12 oz Bag Whole Bean or Ground",
        read={"brand": ("Arturos Coffee & Tea",
                        ["Arturo's Coffee Co.", "Alturas Coffee & Tea", "Arcturus Coffee & Tea"]),
              "tiny": DISTRIBUTOR},
        checks={"origin": False, "weight": False}),
    "pavdomina coffee": dict(
        image="a3bmkb1y4kakxyasqxmw8uffu31m",
        title="Single Origin Papua New Guinea Coffee, Bourbon & Typica, Medium Roast, "
              "Caramel Honey Fruit Notes, 12 oz",
        read={"brand": ("PavDomina Java", ["PaxDomina Java", "PavDomino Java", "Pax Romana Java"]),
              "tiny": DISTRIBUTOR},
        checks={"origin": False, "weight": False}),
    "sage candle": dict(
        image="odfjjkie4k706tfkaskgui4gflxm",
        title="Scented Candle Tin, Coconut Wax Blend, Coastal Sage & Sea Salt, 6 oz Travel Tin, "
              "Herbal Fresh Home Fragrance",
        read={"brand": PRESENT_DAY,
              "name": ("California Sage",
                       ["Coastal Sage & Sea Salt", "California Sagebrush", "Carolina Sage"]),
              "size": ("6 oz", ["8 oz", "4 oz", "16 oz"])},
        checks={"coastal": False, "california": True}),
    "bread candle": dict(
        image="32l5armyahslxlip6eg0sebxkpw0",
        read={"brand": PRESENT_DAY,
              "name": ("Honey Butter Bread", ["Honey Butter Toast", "Brown Butter Bread", "Honey Oat Bread"])}),
    "hot sauce": dict(
        image="pqivexa3msgx3i4pjynxhucko50j",
        read={"brand": ("CW Dressings", ["CV Dressings", "GW Dressings", "CW Seasonings"]),
              "name": ("Sweet Date Hot Sauce",
                       ["Sweet Heat Hot Sauce", "Spicy Date Hot Sauce", "Sweet Date Chili Sauce"]),
              "size": ("8 fl oz (236 mL)", ["8 fl oz (237 mL)", "5 fl oz (148 mL)", "10 fl oz (296 mL)"])}),
    "keto mix": dict(
        image="akrq71wg55uuhb7a4nwsy1adlk1b",
        read={"brand": ("Rosette's", ["Rosetta's", "Roselle's", "Rosemary's"]),
              "name": ("Coconut Macaroon", ["Coconut Macadamia", "Chocolate Macaroon", "Pistachio Shortbread"]),
              "size": ("4.7 oz", ["4.2 oz", "7.4 oz", "14.7 oz"])}),
}


def norm(s):
    return " ".join(re.sub(r"[^a-z0-9.]+", " ", s.lower()).split())


def closeness(phrase, text):
    """Best match of `phrase` against any same-length window of `text`, 0-1."""
    p, t = norm(phrase), norm(text)
    if not t:
        return 0.0
    n = len(p)
    return max(SequenceMatcher(None, p, t[i:i + n]).ratio() for i in range(max(1, len(t) - n + 1)))


def ocr(path, out):
    """Tesseract the way an engineer would have run it: grayscale, upscaled 2x,
    sparse-text and full-page modes combined."""
    img = PILImage.open(path).convert("L")
    big = os.path.join(out, Path(path).stem + "_2x.png")
    img.resize((img.width * 2, img.height * 2), PILImage.LANCZOS).save(big)
    text = []
    for psm in ("11", "3"):
        r = subprocess.run(["tesseract", big, "stdout", "--psm", psm], capture_output=True, text=True)
        text.append(r.stdout)
    return " ".join(" ".join(text).split())


def ocr_pick(options, text):
    best = max(options, key=lambda o: closeness(o, text))
    return (best, closeness(best, text)) if closeness(best, text) >= 0.6 else (None, 0.0)


def ocr_check(name, text):
    if name == "weight":
        # Any figure right after "Net wt": OCR reads o as 0 and g as 9 ("Netwt lIb/160z/4559").
        return bool(re.search(r"net\s*w\w*\W{0,3}.{0,10}\d", text, re.I))
    phrase = {"origin": "Papua New Guinea", "coastal": "Coastal Sage & Sea Salt",
              "california": "California Sage"}[name]
    return closeness(phrase, text) >= 0.8


def main():
    load_dotenv()
    key = os.environ.get("TYPESAFE_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        sys.exit("TYPESAFE_KEY is not set")
    client = TypeSafeClient(api_key=key, headers=IMAGES_ON, timeout=120.0, retry=RetryPolicy(max_retries=3))
    out = os.path.join(tempfile.gettempdir(), "jev_read_labels")
    os.makedirs(out, exist_ok=True)
    has_ocr = shutil.which("tesseract") is not None

    paths, photos = {}, {}
    for name, p in PHOTOS.items():
        paths[name] = os.path.join(out, f"{p['image']}.jpg")
        if not os.path.exists(paths[name]):
            req = urllib.request.Request(CDN + p["image"], headers={"User-Agent": "Mozilla/5.0"})
            with open(paths[name], "wb") as fh:
                fh.write(urllib.request.urlopen(req, timeout=30).read())
        photos[name] = Image(Path(paths[name]))

    # Options shuffled per question, the same way on every run.
    options = {(n, f): random.Random(f"{n}/{f}").sample([t] + d, k=len(d) + 1)
               for n, p in PHOTOS.items() for f, (t, d) in p["read"].items()}

    def read(job):
        name, model = job
        qs = {f: Choice(instructions=QUESTIONS[f][1], criteria={o: None for o in options[(name, f)]})
              for f in PHOTOS[name]["read"]}
        return client.system_one(model=model, state={"photo": photos[name]}, questions=qs)

    def check(job):
        name, model = job
        p = PHOTOS[name]
        qs = {c: Noul(instructions=CHECKS[c]) for c in p["checks"]}
        return client.system_one(model=model, state={"photo": photos[name], "title": p["title"]}, questions=qs)

    jobs = [(n, m) for n in PHOTOS for m in MODELS]
    check_jobs = [(n, m) for n in PHOTOS if PHOTOS[n].get("checks") for m in MODELS]
    with ThreadPoolExecutor(8) as pool:
        reads = dict(zip(jobs, pool.map(read, jobs)))
        checks = dict(zip(check_jobs, pool.map(check, check_jobs)))
        texts = dict(zip(PHOTOS, pool.map(lambda n: ocr(paths[n], out) if has_ocr else "", PHOTOS)))

    tokens = sum(r.usage.input_tokens for r in [*reads.values(), *checks.values()])

    print("\nWHAT TESSERACT TRANSCRIBED (first 110 characters)")
    for n in PHOTOS:
        print(f"  {n:17} {texts[n][:110] or '(nothing)'}")

    print("\nREADING, photo only: ✓ right, ✗ wrong pick, — OCR found nothing close; Jev confidence in brackets")
    heads = [m.replace("jev-", "Jev ") for m in MODELS] + ["Tesseract"]
    print(f"  {'photo':17} {'text':7} {'printed':23} " + "".join(f"{h:28}" for h in heads))
    score = {}
    for n, p in PHOTOS.items():
        for f, (truth, _) in p["read"].items():
            tier = QUESTIONS[f][0]
            cells = []
            for m in MODELS:
                a = reads[(n, m)].choices[f]
                ok = a.choice == truth
                score.setdefault((tier, m), []).append(ok)
                cells.append(f"✓ ({a.confidence:.2f})" if ok else f"✗ {a.choice[:16]} ({a.confidence:.2f})")
            pick, _ = ocr_pick(options[(n, f)], texts[n])
            score.setdefault((tier, "ocr"), []).append(pick == truth)
            cells.append("✓" if pick == truth else ("—" if pick is None else f"✗ {pick[:22]}"))
            print(f"  {n:17} {tier:7} {truth[:22]:23} " + "".join(f"{c:28}" for c in cells))

    print("\n  right, by text size:")
    for tier in ("large", "medium", "small", "tiny"):
        row = []
        for col in MODELS + ["ocr"]:
            s = score[(tier, col)]
            row.append(f"{sum(s)}/{len(s)}")
        print(f"    {tier:7} " + "".join(f"{h}: {v:8}" for h, v in zip(heads, row)))
    tot = lambda col: sum(sum(v) for (t, c), v in score.items() if c == col)  # noqa: E731
    n_q = sum(len(v) for (t, c), v in score.items() if c == "ocr")
    print("    total   " + "".join(f"{h}: {tot(c)}/{n_q:<5}" for h, c in zip(heads, MODELS + ["ocr"])))

    print("\nPHOTO + TITLE: does Jev read the label or repeat the title? Noul = p(yes), 0.5 cut")
    print(f"  {'photo':17} {'question':44} {'label':6} " + "".join(f"{h:13}" for h in heads))
    right = {c: 0 for c in MODELS + ["ocr"]}
    total = 0
    for n, p in PHOTOS.items():
        for c, truth in (p.get("checks") or {}).items():
            total += 1
            cells = []
            for m in MODELS:
                v = checks[(n, m)].nouls[c].noul
                right[m] += (v >= 0.5) == truth
                cells.append(f"{'✓' if (v >= 0.5) == truth else '✗'} {v:.2f}")
            o = ocr_check(c, texts[n])
            right["ocr"] += o == truth
            cells.append(f"{'✓' if o == truth else '✗'} {'yes' if o else 'no'}")
            q = CHECKS[c][len(ONLY_LABEL):]
            print(f"  {n:17} {q[:43]:44} {'yes' if truth else 'no':6} " + "".join(f"{x:13}" for x in cells))
    print("    total " + "".join(f"{h}: {right[c]}/{total}   " for h, c in zip(heads, MODELS + ["ocr"])))
    print(f"\n{tokens:,} input tokens, ${tokens / 1e6 * 0.042:.4f}. Tesseract is free and local.")


if __name__ == "__main__":
    main()
