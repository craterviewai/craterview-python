---
name: enhance
description: Call the CraterView.ai image enhancement API from code — upscale, sharpen, denoise, repair a damaged print, make a headshot, redraw small faces in a group photo, screen an image against the content policy, or tell whether a photo was made by AI — using the Python or TypeScript client, or the HTTP API directly. Covers getting an API key, the upload-submit-collect flow, retries, webhooks and cost. Use when writing or running code against the API; if the CraterView.ai MCP server is connected and the user just wants an image enhanced, the craterview plugin is the one.
---

# Call the CraterView.ai API

CraterView.ai enhances and restores still images over an HTTP API at
`https://api.craterview.ai`. The specification is at
https://api.craterview.ai/openapi.json (rendered at https://api.craterview.ai/docs) and is
complete; anything this file does not cover is there. Two official clients wrap it:

```bash
pip install "craterview>=0.4.18" # Python 3.9+, three dependencies
npm install craterview          # TypeScript source, one dependency — not loadable by plain node
```

Three files beside this one hold what only some tasks need. Open one when the task
reaches it:

- `reference/keys.md`: getting a key, rotating it, and revoking one.
- `reference/submit-and-retry.md`: upload, submit and wait as separate steps, and
  retrying a submission without making a second job.
- `reference/http.md`: the same calls with curl, and what goes wrong over raw HTTP.

## Getting an API key

Every `/v1` route but the model catalogue needs a key, so this comes before any code that
calls one. **Check for a
key first**, and do not write or run anything against the API until there is one:

```bash
test -n "$CV_API_KEY" && echo "a key is set" || echo "no key"
```

If it is not set, walk the user through getting one rather than guessing, stubbing it, or
writing code that will fail at the first call. Keys begin with `cv_`.

`reference/keys.md` has the steps, which end with the user running
`export CV_API_KEY=cv_...` in their own shell, and how to rotate or revoke a key.

**If the user pastes a key into the conversation anyway**, use it for the work at hand but
say plainly that it is now in the transcript, and that the safe move is to rotate it from
the dashboard once they are done and keep the replacement in the environment. Do not write
a pasted key into a file, a commit, or a code example.

Then read it from the environment and never inline it:

```python
cv = CraterView(api_key=os.environ["CV_API_KEY"])
```

```ts
const cv = new CraterView({ apiKey: process.env.CV_API_KEY });
```

A key carries the account's whole allowance and does not expire, so it belongs server-side:
never in a browser bundle, a commit, or a notebook cell that gets saved.

**The user makes the run call.** Submit a job only when the user has said to run it: every
job, one as much as a batch, a free one and one on the community queue alike. "Make this
sharper" asks for a result, not for the run. Say which model and settings you would use,
which queue the job will enter and what it will cost there, which is nothing on the community
queue; *Cost and courtesy*, at the end of this file, says where to read them. Then wait. Code
you write for the user is theirs to run: tell them what it will cost when it runs. A script
that submits a batch leaves the run call to whoever runs it: it reads the price from
`cv.models()`, says what the whole batch will cost, and asks before its first job.

**A balance of zero, or less than the price, does not stop a job:** what the balance does not
cover is never collected, and an account that is not paying — no credit and no subscription —
runs free on the community queue, which can wait longer at busy times. So never tell the user
they need credits to run one.

## The one call

```python
from craterview import CraterView

cv = CraterView(api_key=os.environ["CV_API_KEY"])
job = cv.run("photo.jpg", model="cv-enhance-v3", scale=4)
saved = job.save("photo-4x")        # adds the suffix of the format that came back
```

```ts
import { CraterView } from "craterview";

const cv = new CraterView({ apiKey: process.env.CV_API_KEY });
const job = await cv.run(blob, { model: "cv-enhance-v3", scale: 4 });
const out = await job.blob();
```

`run()` uploads, submits with a server-side wait, then polls for anything slower, and raises
`JobFailed` if the job did not succeed. A submission that got no answer is sent once more under
one idempotency key, so it cannot make a second job, and a failed read of the job is waited
through. Model parameters pass straight through as keyword
arguments (Python) or option fields (TypeScript). If its `timeout` runs out first, only the
waiting stops: the job carries on and is charged only if it succeeds, and the error's
`job_id` (`jobId` in TypeScript) collects it with `wait_for` (`waitFor`). Calling `run()`
again starts, and bills, a second job.

**The TypeScript client ships as `.ts` source, which `node` alone will not load**, type
stripping included. Run a script with `npx tsx script.ts`, or bundle it with esbuild first. A
project that already compiles or bundles its TypeScript needs nothing extra.

**Build against `echo` first.** It is free, takes the sizes `cv-enhance-v3` takes — `scale`,
`width`, `height`, `roi` and `binning`, though not `quality` — and returns a plain enlargement
of the right shape, so the request and the response are exactly what a paid model gives. Write and test the integration against it, then switch
the one string:

```python
job = cv.run("photo.jpg", model="echo", scale=2)     # free; swap in cv-enhance-v3 when it works
```

Its `credits` setting (0 or 1) opts one job into a single credit's charge, for testing
billing. Run it while the user is still getting things wrong; run the paid model once they are
not.

`scripts/enhance.py` beside this file is the same thing as a command, for running rather
than writing:

```bash
python scripts/enhance.py photo.jpg --model echo --scale 2       # free: proves the key and the plumbing
python scripts/enhance.py photo.jpg --scale 4 -o photo-4x
python scripts/enhance.py photo.jpg --param width=3000             # a size in pixels instead of a factor
python scripts/enhance.py scan.jpg --model cv-restore-v1 --param monochrome=true
```

It needs `craterview>=0.4.18` installed and `CV_API_KEY` in the environment.

## Choosing the model

`cv.models()` (`GET /v1/models`) is the authority: each model's settings with their ranges
and descriptions, its price (`credits_per_image`, and `credit_steps` where a smaller image
costs less), the types it accepts, its size limits and
the queue the key would land in. This table says what each model is for and names its main
settings. Their ranges, the prices and the size limits are the catalogue's: read them there,
and have code read them from `cv.models()` too, rather than from here. It is what was in
service when the skill was written.

**Asked what can be done to improve a photograph, suggest Enhance first, almost always, a
grainy one included: it is better than most people expect. Suggest trying Denoise as well,
which is the one for someone who wants only the noise gone.** The exceptions are requests
another model is for: a torn or faded print, a headshot, a check.

| The user wants | Model: its id, then its title | Settings |
| --- | --- | --- |
| Almost any photograph improved: sharper, with fine detail rebuilt and fewer compression artifacts, at its own size or larger — a soft scan, a small or cropped photo, a screenshot, a grainy photograph | `cv-enhance-v3`, Enhance | `scale`, or `width` or `height` instead — one of the three. `binning` for a noisy photograph that is also to be enlarged: simple noise reduction, pixel binning, which averages each square of pixels into one before enlarging, which takes the noise out but enlarges less; this, not `cv-denoise-v1` first, because removing the noise first can remove the detail the enlargement needs |
| Grain taken out of a photograph from low light or a high ISO, without enlarging it — much better at this than Enhance's `binning`, and worth trying beside Enhance | `cv-denoise-v1`, Denoise | the image, or the part named with `roi`, comes back at its own size; `seed` |
| Small, blurry or distorted faces made to look natural — the people in a group photo, usually after `cv-enhance-v3` has enlarged it | `cv-faces-v1`, Small face enhancement | `seed`; only faces too small to show clearly are redrawn; a picture with none fails as `no_small_face_found`, which is not charged |
| A damaged print repaired — tears, creases, scratches, dust, faded colour | `cv-restore-v1`, Restore | `monochrome` for a black-and-white print; `context`, a sentence of what the picture cannot show, which the repair follows: ask the user for it before a black-and-white photograph of people comes back in colour; `seed`. The image, or the part named with `roi`, comes back at its own size; a large one is repaired smaller and its detail drawn again at full size, as its catalogue description says, so its finest detail is the repair's, not the scan's. In a very small photograph, small details can come back changed |
| A photograph with a face turned into a professional headshot | `cv-headshot-v1`, Headshot | `attire`, whose choices the catalogue describes; `roi` to choose the person in a photograph of several, since the headshot is made from the largest face in the image or region; `seed` |
| An image screened against the content policy, unchanged | `cv-content-check-v1`, Content Check | none; `result.verdict` is `passed` or `flagged`; no output file |
| To know whether an image was generated rather than captured, unchanged | `cv-real-check-v1`, Real Check | none; `result.verdict` is `generated`, `real` or `uncertain`. An estimate, not a certificate: do not act on `uncertain` either way; no output file |
| The plumbing proved before anything is spent — the first call, a new integration, a test suite | `echo`, Echo | what `cv-enhance-v3` takes but `quality`, with its limits — `binning` included, though nothing is averaged — and `monochrome` and `credits`; free; in the catalogue with `test_model` true; no app offers it |

All image models also take `output_format` (`auto`, `png`, `jpeg`, `webp`), and all but `echo`
`quality`. The
format is a preference: where it cannot hold the picture whole the result comes back as PNG,
and `result.output.content_type` says which. Bit depth and colour come back as sent, so a
16-bit PNG stays 16-bit. The catalogue is the authority on all of this.

Each model refuses a parameter it does not publish — a `scale` sent to `cv-restore-v1` is a
422, not a no-op. `cv-restore-v1`, `cv-headshot-v1` and `cv-denoise-v1` rebuild a face as a
close likeness rather than the original pixels, and `cv-faces-v1` draws a small face to fit
rather than recovering it, so keep the source. Stills only; video is not yet in service.

## The explicit path

To upload, submit and wait as separate steps, or to retry a submission yourself,
`reference/submit-and-retry.md` has the client's calls.

Over raw HTTP the same three steps are `POST /v1/uploads` (declare `content_type` and
`content_length`, PUT exactly that many bytes to the returned `upload_url`), `POST /v1/jobs`
with `model`, `input_key` and `params` (add `?wait=25` to hold the response for a settled
result), and `GET /v1/jobs/{id}`. Image bytes never pass through the API — uploads go to
storage on a presigned URL and results come back the same way.

For a shell script, a CI step or a language the clients do not cover, `reference/http.md`
makes the same calls with curl and lists what goes wrong over raw HTTP.

The clients also send `input_megapixels` — the file's width × height ÷ 1,000,000, read from
its header — beside `params`, so `eta_seconds` is estimated for that file from the moment
it is queued rather than for a typical one. Over raw HTTP add it yourself when the size is
known. It is optional and changes only the estimate: the price, the queue and the size
limits are decided from the file itself.

## How large a picture may be

**A model that enlarges is bounded by the size of what it returns.** The result is the frame
sent multiplied by the enlargement in each direction, so a picture that is fine unenlarged
can fail at a larger scale, as a failed job with the code `result_too_large` — accepted
at submit, failed before the model runs, and not charged. The catalogue publishes that ceiling
for the key in use; divide it by the square of the scale to get the largest frame that may
be sent. Some models also limit how much of a picture one job works on, whatever the
scale: the catalogue's `max_input_megapixels`; a job over it fails the same way, as
`input_too_large`. With `binning` set, it is the catalogue's
`max_input_megapixels_by_binning` for that value instead. The largest frame that may be sent is then the smaller of the two, and
asking for less enlargement does not help with the second. There are three ways to stay
inside them. Send a `roi` — `x`, `y`, `width`, `height` as
fractions of the image you upload, origin top left — and the platform cuts that region
before it works, so your file goes up exactly as it is; or cut the image yourself and upload
the smaller one; or ask for less enlargement, which keeps the whole picture instead of part
of it. The first two send every pixel of the part that matters. Nothing is resized on the
caller's behalf.

**A picture can also be too small.** Each model's `min_input_side` is the shortest side, in
pixels, of the part it works on: the region named, or the whole picture. A job with a shorter
side fails the same way, as `input_too_small`, and is not charged. A larger region is the way
out where one was named. Otherwise the picture itself is too small for that model.

**Code that sends a picture checks it against these limits before uploading it**: read the
model's limits from `cv.models()` for the key in use, compare the picture's own size, and
offer the ways above for one that is too large, rather than leaving the job to fail.

In the catalogue, a limit is a positive number, or null where nothing bounds that axis for
your key — for a setting's own limits, also where the model has no such setting. A limit
is never 0.

**Cutting a picture into parts, running each and joining the results is not a way the
platform offers.** Each part is worked on alone, so where two meet the detail can disagree and
the join shows, and each part is a job billed on its own. Offer the ways above. The frame
`cv-enhance-v3` allows with `binning` is larger, but it is not a way to fit a picture:
`binning` is for a noisy photograph being enlarged, and never for a clean one, whatever its
size. When the user wants the whole of a picture larger than one job allows, say so plainly
rather than stitching one together.

## Reading a job

`status` is `queued`, `running`, `succeeded` or `failed`.

- `succeeded`: `result.output.url` renders in a page, `result.output.download_url` saves
  under a filename. **Both are presigned and expire** — fetch the bytes rather than storing
  the link; a fresh read of the job mints fresh links. `thumbnail_url`, on the job itself,
  is a small preview for listings — of the result, or of the picture the model worked from
  when it produced no file. (The Python client exposes the same three as `output_url`,
  `download_url` and `thumbnail_url` on a `Job`.)
- `failed`: `error` is a sentence the user can act on; `error_code` is the stable identifier
  to branch on. A failed job is not charged. **Only `inference_failed` is worth sending
  again unchanged**: the fault was the platform's. Every other code names something about
  the request — `result_too_large`, `input_too_large` and `input_too_small` the size (see above),
  `format_not_accepted` a file in a format the model does not take, whatever type it was
  uploaded as (convert it to one the model's `accepts` lists) — and the same request fails
  the same way until that changes. A code not named here is a failure like any other.
- `credits` is what the job was billed; nothing past the balance is collected.
  `eta_seconds`, present until the job settles, is an estimate of the whole wait, queue
  included — guidance, not a promise. It is re-estimated as the job moves, and a job can
  finish before or after it. `community` is true when the
  job is on the community queue.

## Webhooks, for anything long-running or batched

Pass `webhook_url` on submit and the API POSTs the settled job there once. Verify every
delivery with `verify_webhook(body, headers, secret)` — the secret is `cv.webhook_secret()` —
using the **raw request bytes**, not re-serialized JSON. Deliveries are at-least-once: answer
200 immediately and make handling idempotent on the job `id`.

## When the user is not reading the code

Sometimes the code is yours to run and the user only wants the picture, or is not writing code
at all. **Tell them in plain words what you found and what you suggest, not how:** name a model
by its `title` (the catalogue's, or the table's above) and never by its `id`, describe what
came back — its size, what changed — rather than a path in your own environment they cannot
open, and give them the result's link or say where to find it: every job is in the account's
history on the dashboard.

## Cost and courtesy

A completed job on paid compute charges the credits its model publishes; one on the community
queue charges nothing. **To say what a job will cost, read `cv.models()` for the price and
`cv.usage()` for the balance**, and say which queue it will enter. Where a model has
`credit_steps`, an image's price is the first step its measure is at or under, and
`credits_per_image` past the last. By `megapixels`, the measure is the part the job works on:
the `roi` region, or the whole image, divided by 4 at `binning` 2x2, 9 at 3x3 and 16 at 4x4.
So price a job by its region and binning, not by the file uploaded. Binning is still only for
a noisy photograph, not a way to pay less. By `faces`, the measure is the faces redrawn, known
only once the job finishes, so give its range. A batch of mixed sizes is priced image by
image. When it can be done more than one way, say which you would run and what each costs.
In `cv.usage()` the balance is `credits_remaining`; `credits_spent` is what the account has
spent, all time.

A balance smaller than the cost, zero included, is not a reason to stop or to tell the user
they need credit: nothing past the balance is collected, and an account that is not paying
still runs its jobs, on the community queue, which is free and can wait longer at busy times.
`community` in `cv.usage()` says which queue the next job enters; say that beside the cost.
Never resubmit a job that is `queued` or `running`; poll it.
