"""
What Jev 1.14 (released 2026-10-14) adds, tried on our own catalogue, with each
result followed by what it means for us.

    pip install -r requirements.txt
    python scripts/jev_capabilities.py [--only models,text,images,box,long] [--catalog products.csv]

Needs typesafe-sdk 0.7.5 or later for Image and Box.

  models  what jev-latest and jev-preview resolve to today
  text    a two-step category question (group, then type, from the small
          example TAXONOMY below) on jev-1.13.0 vs jev-1.14.0, in English,
          Hebrew and Spanish (1.14's headline gain is multilingual)
  images  the opt-in header; categorising from the photo alone; does the photo
          match its title; photos as the options of a Choice
  box     locate the product in each photo and how much of the frame it fills
  long    a planted product in ~150k tokens of catalogue, with and without the
          long-context opt-in; needs --catalog, a CSV with title and
          description columns, to build the haystack

Images, Box and long context are TypeSafe's "Danger Zone": opt-in per request
by header, and not vouched for in production. Roughly 100 calls and 0.6M input
tokens, about 3 cents at $0.042 per million.
"""

import argparse
import csv
import io
import os
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv
from PIL import Image as PILImage, ImageDraw
from typesafe_sdk import Box, Choice, Image, Noul, RetryPolicy, TypeSafeClient, TypeSafeError

OLD, NEW = "jev-1.13.0", "jev-1.14.0"
IMAGES_ON = {"TypeSafe-Danger-Zone-Images": "true"}
LONG_ON = {"TypeSafe-Danger-Zone-Long-Context": "true"}
PRICE_PER_MTOK = 0.042
CDN = "https://cdn.loyaltie.com/"

# Live listings. Two disagree with their own photo, which is what the images
# section checks: the keto title sells a 5-box bundle and the photo shows one
# pouch; the sage candle's title says Coastal Sage & Sea Salt and its label
# says California Sage. None = the title is ambiguous about it.
PRODUCTS = {
    "coffee": dict(
        image="gastaktjb3yxqa89tqnnaqkt6nju",
        title="Single Origin Papua New Guinea Coffee Beans, Toffee & Cherry Citrus Notes, "
              "Air-Roasted Fresh, 1 lb Bag, Whole Bean or Ground",
        description="Get a sweet, comforting cup with a bright lift: Papua New Guinea single "
                    "origin coffee with toffee richness and cherry-citrus sparkle.",
        same_variant=True, same_count=True),
    "hot sauce": dict(
        image="pqivexa3msgx3i4pjynxhucko50j",
        title="Date-Sweetened Hot Sauce, Sweet Heat Five Pepper Blend, Plant-Based, 8 fl oz",
        description="Get bold, craveable heat without the harsh bite. Naturally sweetened "
                    "with organic Deglet Nour date syrup, built on a five-pepper blend.",
        same_variant=True, same_count=True),
    "sage candle": dict(
        image="odfjjkie4k706tfkaskgui4gflxm",
        title="Scented Candle Tin, Coconut Wax Blend, Coastal Sage & Sea Salt, 6 oz Travel "
              "Tin, Herbal Fresh Home Fragrance",
        description="Bring the Northern California coast home with a crisp, herbal-fresh burn. "
                    "A 6 oz travel tin candle in a coconut wax blend.",
        same_variant=False, same_count=True),
    "keto mix": dict(
        image="akrq71wg55uuhb7a4nwsy1adlk1b",
        title="Keto Cookie Baking Mix Variety Pack, Sugar-Free & Grain-Free, 5-Box Bundle "
              "(Choc Chip, PB, Sugar, Macaroon, Pistachio Shortbread)",
        description="Bake a full cookie-board of keto favorites without the sugar crash. "
                    "This 5-mix bundle delivers grain-free, low-carb classics.",
        same_variant=None, same_count=False),
    # Only used as a look-alike: same brand and tin shape as the sage candle.
    "bread candle": dict(
        image="32l5armyahslxlip6eg0sebxkpw0",
        title="Scented Candle Tin, Coconut Wax Blend, Honey Butter Bread (Bakery Gourmand), "
              "6 oz, 30+ Hr Burn, Reusable Travel Tin"),
}

# Hand translations of the English title and description above, shortened the
# same way in each language.
TRANSLATIONS = {
    "coffee": {
        "en": ("Single Origin Papua New Guinea Coffee Beans, Medium Roast, 1 lb Bag",
               "Rich, delightful flavour with toffee sweetness and bright notes of cherry. "
               "Roasted in small batches."),
        "he": ("פולי קפה חד-זניים מפפואה גינאה החדשה, קלייה בינונית, שקית 450 גרם",
               "טעם עשיר ומענג עם מתיקות של טופי ותווים בהירים של דובדבן. נקלה באצוות קטנות."),
        "es": ("Café en grano de origen único de Papúa Nueva Guinea, tueste medio, bolsa de 450 g",
               "Sabor rico y delicioso con dulzor de toffee y notas brillantes de cereza. "
               "Tostado en pequeños lotes."),
    },
    "hot sauce": {
        "en": ("Date-Sweetened Hot Sauce, Five Pepper Blend, Plant-Based, 8 fl oz",
               "A sweet-heat hot sauce made from five peppers and sweetened only with dates. "
               "No refined sugar, vegan."),
        "he": ("רוטב חריף ממותק בתמרים, תערובת חמישה צ'ילים, טבעוני, 236 מ\"ל",
               "רוטב חריף-מתוק מחמישה סוגי פלפלים, ממותק בתמרים בלבד. ללא סוכר מזוקק, טבעוני."),
        "es": ("Salsa picante endulzada con dátiles, mezcla de cinco chiles, vegetal, 236 ml",
               "Salsa picante dulce hecha con cinco chiles y endulzada solo con dátiles. "
               "Sin azúcar refinada, vegana."),
    },
    "sage candle": {
        "en": ("Scented Candle Tin, Coconut Wax Blend, Coastal Sage & Sea Salt, 6 oz",
               "Hand-poured coconut wax candle in a travel tin with a cotton wick. Notes of "
               "sage, sea salt and driftwood. About 30 hours of burn time."),
        "he": ("נר ריחני בפחית, תערובת שעוות קוקוס, מרווה חופית ומלח ים, 170 גרם",
               "נר שעוות קוקוס שנוצק ביד בפחית נסיעות עם פתיל כותנה. תווים של מרווה, מלח ים "
               "ועץ סחף. כ-30 שעות בעירה."),
        "es": ("Vela aromática en lata, cera de coco, salvia costera y sal marina, 170 g",
               "Vela de cera de coco vertida a mano en lata de viaje con mecha de algodón. "
               "Notas de salvia, sal marina y madera a la deriva. Unas 30 horas de combustión."),
    },
}

# Not in our catalogue when we ran this, so a yes can only come from finding
# it. Check that yours doesn't sell hedgehog treats.
NEEDLE = {"title": "Freeze-Dried Mealworm Treats for Hedgehogs, 3 oz Pouch",
          "description": "High-protein mealworms for pet hedgehogs and sugar gliders."}

# A small example taxonomy in the shape of a real one: groups, then the product
# types inside each. The first question picks the group, the second the type.
TAXONOMY = {
    "Coffee": ["Whole Bean Coffee", "Ground Coffee", "Coffee Pods", "Instant Coffee"],
    "Tea": ["Loose Leaf Tea", "Tea Bags", "Herbal Tea"],
    "Pantry": ["Hot Sauce", "Baking Mixes & Ingredients", "Spices & Seasonings", "Jams & Spreads"],
    "Snacks": ["Freeze-Dried Snacks", "Candy & Confections", "Chips & Crisps"],
    "Home Fragrance": ["Candles", "Reed Diffusers", "Room Sprays", "Wax Melts"],
    "Skin & Body": ["Body Butter & Lotion", "Soap", "Body Oil", "Lip Care"],
    "Supplements": ["Capsules & Tablets", "Powders", "Gummies"],
    "Other": ["Other"],
}
GROUP_Q = "Which product group does this product belong to?"
TYPE_Q = "Which specific product type is this?"

tokens = []  # input tokens per call, for the cost line at the end


def product_state(title, description):
    return {"title": (title or "").strip(), "description": (description or "").strip()}


def classify_primary(client, model, state):
    """Group, then the type inside it: two Choice questions."""
    gcrit = {g: ", ".join(types) for g, types in TAXONOMY.items()}
    r1 = client.system_one(state=state, model=model,
                           questions={"group": Choice(instructions=GROUP_Q, criteria=gcrit)})
    group = r1.answers["group"]
    types = TAXONOMY[group.choice]
    if len(types) == 1:
        return {"primary_group": group.choice, "primary_category": types[0],
                "primary_confidence": group.confidence, "input_tokens": r1.usage.input_tokens}
    r2 = client.system_one(state=state, model=model,
                           questions={"type": Choice(instructions=TYPE_Q, criteria={t: None for t in types})})
    leaf = r2.answers["type"]
    return {"primary_group": group.choice, "primary_category": leaf.choice,
            "primary_confidence": leaf.confidence,
            "input_tokens": r1.usage.input_tokens + r2.usage.input_tokens}


def header(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def means(*lines):
    print()
    for line in lines:
        print(f"  → {line}")


def pmap(fn, items, workers=8):
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(fn, items))


def failure(e):
    return f"{type(e).__name__}: {str(e)[:160]}"


def leaf(m):
    return f"{m['primary_group']} › {m['primary_category']} ({m['primary_confidence']:.2f})"


def classify(client, model, state):
    try:
        m = classify_primary(client, model, state)
    except TypeSafeError as e:
        return {"error": failure(e)}
    tokens.append(m["input_tokens"])
    return m


def ask(client, model, state, questions):
    r = client.system_one(model=model, state=state, questions=questions)
    tokens.append(r.usage.input_tokens)
    return r


def load_photos(out):
    photos, pil = {}, {}
    for name, p in PRODUCTS.items():
        path = os.path.join(out, f"{p['image']}.jpg")
        if not os.path.exists(path):
            req = urllib.request.Request(CDN + p["image"], headers={"User-Agent": "Mozilla/5.0"})
            with open(path, "wb") as fh:
                fh.write(urllib.request.urlopen(req, timeout=30).read())
        photos[name] = Image(Path(path))  # a str is read as image data, not a path
        pil[name] = PILImage.open(path).convert("RGB")
    return photos, pil


def run_models(c):
    header("1. MODELS: what the aliases point at today")
    for m in c["plain"].models.list().models:
        print(f"  {m.name:12} {m.description}")
    for alias in ("jev-latest", "jev-preview"):
        r = ask(c["plain"], alias, {"t": "ping"}, {"q": Noul(instructions="Is `t` a word?")})
        print(f"  {alias:12} → {r.model}")
    means("The release post says jev-latest moves to 1.14 about a week after 2026-10-14, so",
          "code that calls jev-latest changes model on its own. Pin jev-1.13.0 if you need to",
          "control the switch.")


def run_text(c):
    header(f"2. TEXT: two-step category question, {OLD} vs {NEW}, three languages")
    rows = [(name, lang, t, d) for name, langs in TRANSLATIONS.items() for lang, (t, d) in langs.items()]
    jobs = [(row, model) for row in rows for model in (OLD, NEW)]
    got = pmap(lambda j: classify(c["plain"], j[1], product_state(j[0][2], j[0][3])), jobs)
    res = {(j[0][0], j[0][1], j[1]): g for j, g in zip(jobs, got)}

    print(f"  {'product':12} {'lang':4} {OLD:42} {NEW}")
    for name, lang, _, _ in rows:
        a, b = res[(name, lang, OLD)], res[(name, lang, NEW)]
        cell = lambda m: m.get("error") or leaf(m)  # noqa: E731
        print(f"  {name:12} {lang:4} {cell(a)[:41]:42} {cell(b)}")

    ok = {k: v for k, v in res.items() if "error" not in v}
    print()
    for model in (OLD, NEW):
        stable = sum(1 for name in TRANSLATIONS
                     if len({ok[(name, lang, model)]["primary_category"]
                             for lang in TRANSLATIONS[name] if (name, lang, model) in ok}) == 1)
        conf = {lang: mean(ok[(n, lang, model)]["primary_confidence"]
                           for n in TRANSLATIONS if (n, lang, model) in ok) for lang in ("en", "he", "es")}
        print(f"  {model}: same category in all three languages for {stable}/{len(TRANSLATIONS)} products;"
              f" mean confidence en {conf['en']:.2f}  he {conf['he']:.2f}  es {conf['es']:.2f}")
    agree = sum(1 for name, lang, _, _ in rows
                if (name, lang, OLD) in ok and (name, lang, NEW) in ok
                and ok[(name, lang, OLD)]["primary_category"] == ok[(name, lang, NEW)]["primary_category"])
    print(f"  {OLD} and {NEW} pick the same category on {agree}/{len(rows)} rows")
    means("Our catalogue is English, so the multilingual gain matters only once we take on",
          "non-English stores. A lower he/es confidence means more of those products would",
          "fall under a review threshold.",
          "Nine rows say nothing about accuracy: before relying on a new version, rerun a",
          "hand-labelled sample of your own on both and compare.")


def run_images(c, photos):
    header("3. IMAGES (opt-in header TypeSafe-Danger-Zone-Images: true)")

    print("  a) Same image request, without the header:")
    try:
        ask(c["plain"], NEW, {"photo": photos["coffee"]}, {"q": Noul(instructions="Is `photo` a coffee bag?")})
        print("     accepted (the docs say this is a 400)")
    except TypeSafeError as e:
        print(f"     rejected, {failure(e)}")

    print("\n  b) Category from the text vs from the photo alone (no title, no description):")
    names = ["coffee", "hot sauce", "sage candle", "keto mix"]
    jobs = ([("text", n, NEW) for n in names] + [("photo", n, NEW) for n in names]
            + [("photo", n, OLD) for n in names])

    def go(job):
        kind, name, model = job
        p = PRODUCTS[name]
        state = (product_state(p["title"], p["description"]) if kind == "text"
                 else {"photo": photos[name]})
        return classify(c["images"], model, state)

    res = dict(zip(jobs, pmap(go, jobs)))
    cell = lambda m: m.get("error") or leaf(m)  # noqa: E731
    for n in names:
        t, p14, p13 = res[("text", n, NEW)], res[("photo", n, NEW)], res[("photo", n, OLD)]
        same = ("" if "error" in p14 or "error" in t else
                "  ✓ same" if p14["primary_category"] == t["primary_category"] else "  ✗ differs")
        print(f"     {n:12} text {NEW}:  {cell(t)}")
        print(f"     {'':12} photo {NEW}: {cell(p14)}{same}")
        print(f"     {'':12} photo {OLD}: {cell(p13)}")

    print("\n  c) Does the photo match its own title? Noul = probability of yes."
          "\n     One combined question, then the same thing split in two, as the docs advise.")
    qs = {
        "matches": Noul(instructions="Does `photo` show the product described in `title`, with the "
                                     "same flavour or scent and the same pack count?"),
        "variant": Noul(instructions="Does the label in `photo` name the same flavour or scent as `title`?"),
        "count": Noul(instructions="Does `photo` show as many units as `title` says the listing includes?"),
    }
    out = pmap(lambda n: ask(c["images"], NEW, {"photo": photos[n], "title": PRODUCTS[n]["title"]}, qs), names)
    print(f"     {'product':12} {'matches':>8} {'variant':>8} {'count':>8}   expected (variant, count)")
    for n, r in zip(names, out):
        p = PRODUCTS[n]
        exp = lambda v: "?" if v is None else ("yes" if v else "no")  # noqa: E731
        print(f"     {n:12} {r.nouls['matches'].noul:8.2f} {r.nouls['variant'].noul:8.2f} "
              f"{r.nouls['count'].noul:8.2f}   {exp(p['same_variant'])}, {exp(p['same_count'])}")

    print("\n  d) Photos as the options of a Choice: which photo shows the product in `title`?"
          "\n     Options are named A-D so the names give nothing away; A and B are look-alike tins.")
    options = {"A": "sage candle", "B": "bread candle", "C": "hot sauce", "D": "coffee"}
    q = {"pick": Choice(instructions="Which option is a photo of the product described in `title`?",
                        criteria={k: photos[v] for k, v in options.items()})}
    titles = ["sage candle", "bread candle", "hot sauce", "coffee"]
    picks = pmap(lambda n: ask(c["images"], NEW, {"title": PRODUCTS[n]["title"]}, q).choices["pick"], titles)
    for n, a in zip(titles, picks):
        right = options[a.choice] == n
        probs = " ".join(f"{k}={v:.2f}" for k, v in sorted(a.probabilities.items()))
        print(f"     {n:12} → {a.choice} ({options[a.choice]}) {'✓' if right else '✗'}   {probs}")

    means("(b) If the photo-only category agrees with the text one, images could categorise",
          "products that arrive with a thin or missing description.",
          "(c) This is the useful one for feeds: a title/photo mismatch is a policy problem on",
          "Google and Meta (keto: 5-box bundle, one pouch shown; sage candle: the label says",
          "California Sage). Watch whether the split questions separate better than the combined one.",
          "(d) Images in criteria let Jev pick the right photo among a product's extra images.",
          "All of this needs jev-1.14.0 pinned, the header on every call, and an eval of our own first.")


def run_box(c, photos, pil, out):
    header("4. BOX: one bounding box per question, jev-1.14.0 only")
    targets = {
        "coffee": "the coffee bag in `photo`",
        "hot sauce": "the bottle in `photo`",
        "sage candle": "the candle tin in `photo`",
        "keto mix": "the product package in `photo`",
        "bread candle": "the candle tin in `photo`, not its separate lid",
    }
    try:
        ask(c["images"], OLD, {"photo": photos["coffee"]}, {"b": Box(instructions=targets["coffee"])})
        print(f"  {OLD} accepted a Box question (the docs say Box is 1.14 only)")
    except TypeSafeError as e:
        print(f"  {OLD} with Box: {failure(e)}")

    names = list(targets)
    answers = pmap(lambda n: ask(c["images"], NEW, {"photo": photos[n]},
                                 {"b": Box(instructions=targets[n])}).boxes["b"], names)
    print(f"\n  {'product':12} {'left':>5} {'top':>5} {'right':>5} {'bottom':>6} {'conf':>5} {'fills':>6}   pixels")
    sheet = PILImage.new("RGB", (300 * len(names), 300), "white")
    fills = []
    for i, (n, a) in enumerate(zip(names, answers)):
        b, img = a.box, pil[n].copy()
        w, h = img.size
        px = (round(b.left * w), round(b.top * h), round(b.right * w), round(b.bottom * h))
        fill = (b.right - b.left) * (b.bottom - b.top)
        fills.append(fill)
        conf = "-" if a.confidence is None else f"{a.confidence:.2f}"
        print(f"  {n:12} {b.left:5.2f} {b.top:5.2f} {b.right:5.2f} {b.bottom:6.2f} {conf:>5} {fill:6.0%}   {px}")
        img.crop(px).save(os.path.join(out, f"crop_{n.replace(' ', '_')}.jpg"))
        ImageDraw.Draw(img).rectangle(px, outline="red", width=6)
        sheet.paste(img.resize((300, 300)), (300 * i, 0))
    sheet.save(os.path.join(out, "boxes.png"))
    print(f"\n  boxes drawn on each photo: {os.path.join(out, 'boxes.png')}  (crops alongside)")
    means(f"Coordinates are fractions of the image from the top-left corner; multiply by the",
          f"width/height for pixels. Our product fills {min(fills):.0%}–{max(fills):.0%} of the frame here.",
          "Google's image guidance asks for 75–90%, so Box could drive an automatic tighter crop",
          "for the Google, Meta and Pinterest feeds, or flag photos where the product is too small.",
          "Check boxes.png before trusting the numbers.")


def run_long(c, catalog):
    header("5. LONG CONTEXT (opt-in header TypeSafe-Danger-Zone-Long-Context: true)")
    if not catalog:
        print("  skipped: pass --catalog products.csv (title and description columns) for the haystack")
        return
    products = list(csv.DictReader(open(catalog, newline="", encoding="utf-8")))
    items = [{"title": p["title"], "description": (p.get("description") or "")[:300]} for p in products]

    def catalogue(chars, needle):
        picked, n = [], 0
        for it in items:
            picked.append(it)
            n += len(it["title"]) + len(it["description"])
            if n >= chars:
                break
        if needle:
            picked.insert(len(picked) // 2, NEEDLE)
        return picked

    q = {"q": Noul(instructions="Does `catalog` list a treat made for hedgehogs?")}
    runs = [("small, default limits", 60_000, c["plain"]),
            ("large, no opt-in", 600_000, c["plain"]),
            ("large, opt-in", 600_000, c["long"])]
    print(f"  {'run':24} {'products':>8} {'tokens':>8}   p(yes) planted / p(yes) not planted")
    for label, chars, client in runs:
        cells = []
        for needle in (True, False):
            cat = catalogue(chars, needle)
            try:
                r = ask(client, NEW, {"catalog": cat}, q)
                cells.append((r.nouls["q"].noul, r.usage.input_tokens, len(cat)))
            except TypeSafeError as e:
                cells.append((failure(e), None, len(cat)))
            if label == "large, no opt-in":
                break  # the one call is enough to show the limit
        n, toks = cells[0][2], cells[0][1]
        vals = " / ".join(v if isinstance(v, str) else f"{v:.2f}" for v, _, _ in cells)
        print(f"  {label:24} {n:8} {toks or '-':>8}   {vals}")
    means("The default limits are 64k tokens per request and 32k per question; with the header",
          "both become 262k. Planted should be near 1 and not-planted near 0; a gap that",
          "shrinks on the large run is the reliability loss TypeSafe warns about.",
          "Short inputs are the reliable default. Long context would allow whole-store",
          "questions (duplicates, store-level checks) at proportional cost.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="models,text,images,box,long")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "jev_capabilities"))
    ap.add_argument("--catalog", help="CSV with title and description columns, for the long-context test")
    args = ap.parse_args()
    only = set(args.only.split(","))
    os.makedirs(args.out, exist_ok=True)

    load_dotenv()
    key = os.environ.get("TYPESAFE_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        sys.exit("TYPESAFE_KEY is not set")
    make = lambda h=None: TypeSafeClient(api_key=key, headers=h, timeout=120.0,  # noqa: E731
                                         retry=RetryPolicy(max_retries=3))
    c = {"plain": make(), "images": make(IMAGES_ON), "long": make(LONG_ON)}

    if "models" in only:
        run_models(c)
    if "text" in only:
        run_text(c)
    if only & {"images", "box"}:
        photos, pil = load_photos(args.out)
        if "images" in only:
            run_images(c, photos)
        if "box" in only:
            run_box(c, photos, pil, args.out)
    if "long" in only:
        run_long(c, args.catalog)

    total = sum(tokens)
    print(f"\n{total:,} input tokens, ${total / 1e6 * PRICE_PER_MTOK:.4f} at "
          f"${PRICE_PER_MTOK}/Mtok (output tokens are free)")


if __name__ == "__main__":
    main()
