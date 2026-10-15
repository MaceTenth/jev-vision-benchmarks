# Jev Vision Benchmarks

Experiments comparing image and text-reading capabilities of Jev (TypeSafe) with
general-purpose vision models and conventional moderation/OCR tools. The
benchmarks use product catalog photos and focus on practical marketplace tasks:
moderation, matching a photo to its listing, product classification, and
reading small package labels.

## Image moderation benchmark

The main benchmark runs the same 60 product photos through configurable
moderation engines. It measures sequential latency, parallel throughput, token
use, estimated cost, and agreement with hand-written labels. The dataset has
15 policy-relevant examples and 45 randomly selected catalog photos. The
scored results and labels are included under `data/moderation/`.

The checked-in results were measured on October 15, 2026. For the detailed
methodology, per-question results, caveats, and pricing assumptions, see
[`article/MODERATION.md`](article/MODERATION.md).

| Engine | Median latency | p95 latency | Images/s (8 parallel) | Cost / 1M images | Caught |
| --- | ---: | ---: | ---: | ---: | ---: |
| Jev 1.14 | 376 ms | 510 ms | 13.1 | $123 | 7 / 8 |
| gpt-6-luna | 1,686 ms | 3,237 ms | 3.7 | $153 | 8 / 8 |
| gpt-5.4-nano | 964 ms | 1,279 ms | 5.8 | $313 | 5 / 8 |
| gpt-5.4-mini | 1,471 ms | 1,939 ms | 4.6 | $1,170 | 7 / 8 |
| gpt-5.5 | 1,185 ms | 1,609 ms | 5.2 | $7,797 | 8 / 8 |
| AWS Rekognition moderation | 409 ms | 591 ms | 10.5 | $1,000 | 0 / 1 supported |
| OpenAI omni-moderation | 591 ms | 792 ms | 10.7 | Free | Not applicable |

These are results on a small, curated sample, not a general accuracy claim.
The fixed-taxonomy services do not support all five benchmark questions.
Details and limitations are in the article.

## Setup

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Add credentials for the services you intend to call to `.env`:

- `TYPESAFE_KEY` for Jev experiments (or `TYPESAFE_API_KEY`).
- `OPENAI_API_KEY` for GPT vision models and OpenAI omni-moderation.
- AWS credentials from the standard AWS credential chain for Rekognition.
  `AWS_REGION` is shown in `.env.example`; the moderation benchmark currently
  configures Rekognition in `us-east-1`.

Never commit `.env` or API keys. The repository ignores `.env` files.

## Reproduce the moderation results

Score the checked-in benchmark outputs without making API calls:

```bash
python scripts/moderation_score.py \
  data/moderation/results \
  data/moderation/labels.json \
  data/moderation/results/gpt-6-luna
```

Run a smaller live test first (downloads the selected images and calls the
requested providers):

```bash
python scripts/moderation_bench.py --limit 3 --engines jev-1.14.0
```

Run the full benchmark with the configured providers:

```bash
python scripts/moderation_bench.py \
  --workers 8 \
  --out runs/moderation \
  --engines jev-1.14.0,gpt-5.4-nano,gpt-5.4-mini,gpt-5.5,rekognition,omni-moderation
python scripts/moderation_bench.py \
  --workers 8 \
  --out runs/moderation-luna \
  --engines gpt-6-luna
```

Results are written to `calls.csv` and `summary.json` in the selected output
directory. Outputs under `runs/` are ignored by Git. Use `--images`, `--limit`,
`--workers`, `--engines`, and `--out` to adjust the run. The model names in the
engine list are passed to the OpenAI client unless they match the built-in Jev,
Rekognition, or omni-moderation engines.

## Other experiments

All outputs default to temporary directories unless otherwise noted.

- **Catalog photo audit:** compare product titles with their photos and flag
  possible type, variant, or quantity mismatches. Input CSV needs `title` and
  `image_url` columns; `item_id` and `product_url` are optional.
  ```bash
  python scripts/jev_photo_audit.py --catalog products.csv --sample 200 --seed 1
  ```
- **Jev capabilities:** try model aliases, multilingual product classification,
  image questions, image boxes, and (with a catalog file) long-context
  behavior. Long-context and image requests use TypeSafe opt-in headers.
  ```bash
  python scripts/jev_capabilities.py --only models,text,images,box
  python scripts/jev_capabilities.py --only long --catalog products.csv
  ```
- **Reading package labels:** compare Jev's multiple-choice reading against
  Tesseract OCR on the same photos. Install the `tesseract` executable
  separately to enable the OCR comparison; the script can still run without
  it.
  ```bash
  python scripts/jev_read_labels.py
  ```

## Data, privacy, and limitations

The moderation images are referenced by public CDN URLs in
`data/moderation/images.csv`; the image files themselves are downloaded when a
benchmark runs. Running an experiment sends image and/or catalog data to the
selected model providers and may incur usage charges. Review provider terms
and use data you are authorized to submit.

Jev image and long-context inputs are marked as TypeSafe "Danger Zone" features
in these experiments and are explicitly enabled by request headers. Treat
model flags as candidates for human review, not as automatic listing edits or
policy decisions. Results depend on this dataset, prompts, model versions,
network, and provider pricing; validate against your own labeled examples
before relying on them.
