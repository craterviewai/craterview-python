---
name: enhance-image
description: Call the CraterView image enhancement API from code — upscale, sharpen, de-noise, repair a damaged print, make a headshot, or screen an image against the content policy — using the Python or TypeScript client, or the HTTP API directly. Covers getting an API key, the upload-submit-collect flow, retries, webhooks and cost. Use when writing or running code against the API; if the CraterView MCP server is connected and the user just wants an image enhanced, the craterview plugin is the one.
---

# Call the CraterView API

CraterView enhances and restores still images over an HTTP API at
`https://api.craterview.ai`. The specification is at
https://api.craterview.ai/openapi.json (rendered at https://api.craterview.ai/docs) and is
complete; anything this file does not cover is there. Two official clients wrap it:

```bash
pip install craterview          # Python 3.9+, one dependency
npm install craterview          # TypeScript source, zero dependencies
```

## Getting an API key

Every `/v1` route needs a key, so this comes before any code that calls one. **Check for a
key first**, and do not write or run anything against the API until there is one:

```bash
test -n "$CRATERVIEW_API_KEY" && echo "a key is set" || echo "no key"
```

If it is not set, walk the user through getting one rather than guessing, stubbing it, or
writing code that will fail at the first call. Keys begin with `cv_`.

1. **Sign in at https://craterview.ai.** The workspace hands out a key without signing in,
   but that account exists only in that browser's cookie — signing in first is what makes
   the key belong to an account they can come back to, rotate, and pay into.
2. **Open the dashboard, https://craterview.ai/dashboard, and the _Developer API_ panel.**
   It is closed until its heading is clicked.
3. **Press _Show my API key_, then _Copy_.** This is the same key the web app itself is
   using, so code holding it calls exactly what the page calls.
4. **Ask them to export it in the shell they will run the code from** — not to paste it
   here:

   ```bash
   export CRATERVIEW_API_KEY=cv_...
   ```

   Offer to add that line to their shell profile if they want it to persist. If the key
   should live with a project instead, `.env` beside the code is the usual place — confirm
   the file is ignored by version control before it goes in.

**If the user pastes a key into the conversation anyway**, use it for the work at hand but
say plainly that it is now in the transcript, and that the safe move is to rotate it from
the dashboard once they are done and keep the replacement in the environment. Do not write
a pasted key into a file, a commit, or a code example.

Then read it from the environment and never inline it:

```python
cv = CraterView(api_key=os.environ["CRATERVIEW_API_KEY"])
```

```ts
const cv = new CraterView({ apiKey: process.env.CRATERVIEW_API_KEY });
```

A key carries the account's whole allowance and does not expire, so it belongs server-side:
never in a browser bundle, a commit, or a notebook cell that gets saved.

**Rotating replaces the key immediately** — the old one stops at once, and any assistant
connected to the account is disconnected with it. Tell the user that before they press it.
Several keys on one account is the ordinary arrangement (`cv.create_key("staging")`,
`cv.revoke_key(...)`), and rotating without downtime means creating the new key, moving
clients onto it, then revoking the old.

A new account runs jobs with no credit at all: they go to the community queue, served after
paid work, so they wait longer rather than being refused. Credits buy priority, not access.

## The one call

```python
from craterview import CraterView

cv = CraterView(api_key=os.environ["CRATERVIEW_API_KEY"])
job = cv.run("photo.jpg", model="cv-enhance-v3", scale=4)
job.save("photo-4x.png")
```

```ts
import { CraterView } from "craterview";

const cv = new CraterView({ apiKey: process.env.CRATERVIEW_API_KEY });
const job = await cv.run(blob, { model: "cv-enhance-v3", scale: 4 });
const out = await job.blob();
```

`run()` uploads, submits with a server-side wait, then polls for anything slower, and raises
`JobFailed` if the job did not succeed. Model parameters pass straight through as keyword
arguments (Python) or option fields (TypeScript).

`scripts/enhance.py` beside this file is the same thing as a command, for running rather
than writing:

```bash
python scripts/enhance.py photo.jpg --scale 4 -o photo-4x.png
python scripts/enhance.py scan.jpg --model cv-restore-v1 --param mode=full --param monochrome=true
```

It needs `pip install craterview` and `CRATERVIEW_API_KEY` in the environment.

## Choosing the model

`cv.models()` (`GET /v1/models`) is authoritative: it publishes each model's parameter
schema, price, accepted types and size limit, and the queue the key would land in. Read it
rather than trusting this table when the answer matters; this is what was in service when the
skill was written.

| The user wants | Model | Parameters |
| --- | --- | --- |
| Sharper, larger, less noise, fewer compression artifacts — a soft scan, a small or cropped photo, a screenshot | `cv-enhance-v3` | `scale` 1–4 (default 4) |
| A damaged print repaired — tears, creases, scratches, dust, faded colour | `cv-restore-v1` | `mode` `full` or `spots` (spots repairs dust and hairline scratches only and keeps every other pixel); `monochrome` for a black-and-white print; `size` `standard` (about one megapixel) or `large` (2048 px long side, several times slower); `seed` |
| A photograph with a face turned into a professional headshot | `cv-headshot-v1` | `seed` |
| An image screened against the content policy, unchanged | `cv-content-check-v1` | none; free; the answer is in `result` and there is no output file |

All image models also take `output_format` (`auto`, `png`, `jpeg`, `webp`; `auto` returns
the format sent, except that HEIC/HEIF comes back as JPEG) and `quality` (96–100, for jpeg
and webp).

Each model refuses a parameter it does not publish — a `scale` sent to `cv-restore-v1` is a
422, not a no-op. `cv-restore-v1` and `cv-headshot-v1` rebuild a face as a close likeness
rather than the original pixels, so keep the source. Stills only; video is not yet in service.

## The explicit path

When the caller wants to hold the key, submit later, fan out, or not block:

```python
input_key = cv.upload("photo.jpg")                       # → "inputs/..."
job = cv.submit("cv-enhance-v3", input_key, scale=4)     # 202, status queued
job = cv.wait_for(job, timeout=600)                       # or: cv.job(job.id) yourself
data = job.bytes()
```

Over raw HTTP the same three steps are `POST /v1/uploads` (declare `content_type` and
`content_length`, PUT exactly that many bytes to the returned `upload_url`), `POST /v1/jobs`
with `model`, `input_key` and `params` (add `?wait=25` to hold the response for a settled
result), and `GET /v1/jobs/{id}`. Image bytes never pass through the API — uploads go to
storage on a presigned URL and results come back the same way.

## Reading a job

`status` is `queued`, `running`, `succeeded` or `failed`.

- `succeeded`: `output_url` renders in a page, `download_url` saves under a filename. **Both
  are presigned and expire** — fetch the bytes rather than storing the link; a fresh read of
  the job mints fresh links. `thumb_url` is a small preview for listings.
- `failed`: `error` is a sentence the user can act on; `error_code` is the stable identifier
  to branch on. A failed job is not charged.
- `credits` is what the job was billed. `eta_seconds`, present until the job settles, covers
  the whole wait, queue included. `community` is true when the job is on the community queue.

## Retries: the client does not, so the caller must do it right

Neither client retries, because only the caller knows where one logical request ends. Make
one idempotency key per logical request and reuse it for every attempt:

```python
from craterview import new_idempotency_key
key = new_idempotency_key()                      # once
for attempt in range(3):
    try:
        job = cv.submit("cv-enhance-v3", input_key, idempotency_key=key, scale=4)
        break
    except (ConnectionError, TimeoutError):
        continue                                 # same key → at most one job is ever created
```

A fresh key per attempt starts a job per attempt and bills each. The same key with a
different body is a 409. A `RateLimited` (429) carries `retry_after` in seconds.

## Webhooks, for anything long-running or batched

Pass `webhook_url` on submit and the API POSTs the settled job there once. Verify every
delivery with `verify_webhook(body, headers, secret)` — the secret is `cv.webhook_secret()` —
using the **raw request bytes**, not re-serialized JSON. Deliveries are at-least-once: answer
200 immediately and make handling idempotent on the job `id`.

## Cost and courtesy

Each completed job charges the credits its model publishes. Before running a batch, read
`cv.models()` for the price and `cv.usage()` for the balance, say what it will cost, and do
not submit until the user agrees. Never resubmit a job that is `queued` or `running`; poll it.
