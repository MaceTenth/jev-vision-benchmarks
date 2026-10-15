# The free moderation APIs saw nothing. The LLMs saw too much.

## Seven ways to moderate product photos, measured on our own catalog for latency, cost and mistakes

Every marketplace eventually needs image moderation, and almost every
moderation API is built for a different problem than ours.

The classic services answer "is there nudity, violence or gore in this
upload?" That is the right question for a social network. For a product
catalog that feeds Google, Meta and Pinterest, the questions that get listings
rejected are different:

- Is there cannabis or CBD branding on the pack?
- Is this a smoking product?
- Does the label claim to cure, treat or prevent a disease?
- Has someone stamped "SALE" or "free shipping" on the photo?

So we took 60 photos from our live catalog and asked those questions five ways:
two fixed-category moderation APIs, four general vision LLMs, and Jev, a
System One model. We measured every engine on the same images. The labels were
written **before looking at any engine's answer**.

---

## Results

60 photos: 15 picked because they were likely to trip a policy (CBD
products, herbal pre-roll joints, libido supplements, a "What Are You
Smoking?" BBQ T-shirt), and 45 drawn at random from the catalog. Each engine
ran once sequentially to measure latency, then again with 8 requests in
parallel to measure throughput.

| | can ask our 5 questions | median latency | p95 | images/s (8 parallel) | cost per 1M images | caught (of 8) | false alarms (of 286) |
|---|---|---|---|---|---|---|---|
| **Jev 1.14** (System One) | yes | **376 ms** | **510 ms** | **13.1** | **$123** | 7 | **1** |
| **gpt-6-luna** | yes | 1,686 ms | 3,237 ms | 3.7 | $153 | **8** | 5 |
| **gpt-5.4-nano** | yes | 964 ms | 1,279 ms | 5.8 | $313 | 5 | 3 |
| **gpt-5.4-mini** | yes | 1,471 ms | 1,939 ms | 4.6 | $1,170 | 7 | 6 |
| **gpt-5.5** | yes | 1,185 ms | 1,609 ms | 5.2 | $7,797 | **8** | 2 |
| **AWS Rekognition** moderation | 2 of 5 | 409 ms | 591 ms | 10.5 | $1,000 | 0 of 1 | 0 of 118 |
| **OpenAI omni-moderation** | 1 of 5 | 591 ms | 792 ms | 10.7 | **free** | – | 0 of 60 |

Cost per image is the measured tokens multiplied by each provider's published
price on 2026-10-15, at the standard tier. Jev used 2,919 input tokens per photo
for all five questions; the GPT models used about 1,367 input and 32 output
tokens.

**Our whole catalog, 3,788 photos, costs $0.47 with Jev, $0.58 with
gpt-6-luna, $3.79 with Rekognition and $29.53 with gpt-5.5.** At our size,
price is not the deciding factor. At a million photos a month it is.

---

## Finding 1: the fixed-category APIs returned nothing

Rekognition and omni-moderation **returned no labels on any of the 60 photos.**
That includes six CBD products, all with "CBD" or "THC" printed on the label,
and a three-pack of herbal pre-roll joints.

This is not a failure. They do what they were built for. OpenAI's endpoint
checks images only for sexual content, violence and self-harm; its drugs
category is text-only. Rekognition has a "Drugs & Tobacco" class, and it still
did not fire on a tin labelled "Herbal Joints".

If your risk is nudity in user uploads, use omni-moderation: it is free and
fast. If your risk is catalog policy, these APIs are not asking your questions,
and a clean result from them means nothing.

---

## Finding 2: flexibility costs latency, and the price range is 50x

Any vision LLM can answer a policy question written in plain English. That is
the whole appeal. The costs:

- **Latency.** Every GPT model was 2.5–4.5x slower than Jev at the median.
  The cheapest, gpt-6-luna, had a p95 of 3.2 seconds.
- **Price spread.** From $153 to $7,797 per million images, about 50x,
  between models that caught nearly the same things.
- **Over-reading.** The LLMs' false alarms came from reading product names as
  medical claims ("Healing Salve", "Pain Relief Balm", "Heart Support Tea",
  "Head Ease"). Three of the four also flagged the BBQ "What Are You Smoking?"
  T-shirt as a smoking product.

We ran every GPT model at reasoning effort `none`, its fastest setting. More
reasoning might cut the false alarms, but it would add latency and output
tokens.

---

## Finding 3: Jev was fastest and cheapest, and it tells you when it is unsure

Jev answered all five questions for $123 per million photos, at 13 photos a
second with 8 parallel requests, with one false alarm in 286.

It missed one case: a tincture label reading "analgesic … anti-inflammatory",
which it scored 0.45. Its one false alarm scored 0.50. **Both errors sat at
the boundary.**

That is the useful part. Jev returns a probability for each yes/no question,
not a written "true". We routed every answer between 0.3 and 0.7 to a person:

- **4 of 294 answers** (1.4%) went to review, including both errors.
- **The other 290 were all correct.**

An LLM returning plain true/false cannot be routed like this. The only way
to find its mistakes is to review everything it said yes to.

---

## Where every engine disagreed with us

One photo was flagged as a health claim by **every** engine: a serum whose
label says it "will prevent wrinkles". We labelled that cosmetic, not medical;
a regulator might disagree. We kept our label, but this case is about policy,
not about any one model. Whatever engine you use, write your definitions down
with examples. Jev takes them as criteria; an LLM takes them in the prompt.

---

## Prices we did not measure

No API keys, so published prices only (2026-10-15), not tested on our photos.
The LLM rows are estimates from each provider's image-token rule plus our
prompt.

| | cost per 1M images | note |
|---|---|---|
| Azure AI Content Safety, image | $750 | Standard tier, East US; fixed categories |
| Google Cloud Vision SafeSearch | $1,500 ($600 above 5M/month) | fixed categories |
| Hive Visual Moderation | $3,000 | self-serve; volume pricing via sales |
| Sightengine | ≈ $6,000–8,000 | $0.002 per operation; a product check is 3–4 operations |
| Claude Haiku 5.5 | ≈ $175 | 1,156 image tokens per 950×950 photo at $0.10 per 1M |
| Gemini 3.1 Flash-Lite | ≈ $410 | 1,120 image tokens at $0.25 per 1M |

Every LLM provider here offers a **batch tier at 50% off**, with results in
hours, not seconds. That makes gpt-6-luna about $77 per million images,
cheaper than Jev's standard rate, for moderation that can wait overnight.

---

## What we would do

- **Product catalog (our case):** Jev with a 0.3–0.7 review band. It is
  fastest, cheapest at real-time rates, has the fewest false alarms, and it
  tells us which answers to check.
- **User uploads where nudity and violence are the risk:** omni-moderation
  first. It is free.
- **Overnight jobs on a strict budget:** a cheap LLM on the batch tier,
  accepting more false alarms to review.
- **Whatever you choose:** label 50 of your own photos before you trust any
  number on a pricing page, including ours.

---

## Caveats

- **Small sample.** 60 photos and 8 positive cases. The gaps in "caught" are
  one to three photos, which is not statistically meaningful. The false-alarm
  counts over 286 negatives say more, but are still small. No photo showed
  nudity or a clear promotional overlay, so those questions measured false
  alarms only.
- **Labels.** One labeller, blind to engine output: Claude, the AI assistant
  that ran this benchmark, looking at each photo at full resolution. Claude is
  also an Anthropic model, and Anthropic's Claude Haiku is priced (not
  measured) above. 6 ambiguous cases were left out of scoring.
- **Clean photos.** These are studio shots from curated sellers. User uploads
  are messier, and every engine will do worse on them.
- **Latency.** Measured from one machine on one network, so provider region
  matters. We did not test rate limits beyond 8 parallel requests.
- **Jev status.** Jev's image input is opt-in "Danger Zone": TypeSafe does not
  yet vouch for it in production.
- **Not measured:** Claude, Gemini, Azure, Google Vision, Hive and Sightengine
  are priced only.

---

## Reproduce it

```bash
pip install -r requirements.txt

# score the shipped results
python scripts/moderation_score.py data/moderation/results data/moderation/labels.json \
    data/moderation/results/gpt-6-luna

# rerun the benchmark (writes to runs/, which git ignores)
python scripts/moderation_bench.py --workers 8 --out runs/moderation \
    --engines jev-1.14.0,gpt-5.4-nano,gpt-5.4-mini,gpt-5.5,rekognition,omni-moderation
python scripts/moderation_bench.py --workers 8 --out runs/moderation-luna --engines gpt-6-luna
```

The 60 photos are listed in `data/moderation/images.csv`, the labels are in
`data/moderation/labels.json`, and raw per-call results are in
`data/moderation/results/`.
