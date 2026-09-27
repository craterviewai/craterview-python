---
name: enhance-image
description: Sharpen, enlarge, de-noise or repair a photograph with CraterView's hosted tools (enhance_image, get_job) — "upscale this", "make it less blurry", "fix this old photo", "clean up this screenshot". Use when the CraterView server is connected; if the user wants to call the API from their own code instead, the craterview-api plugin is the one.
---

# Enhance an image with CraterView

CraterView runs the work; you choose the model, hand over the image, and relay the result.
The server exposes two tools:

- `enhance_image` — upload one image and run a model on it. Most images finish inside the
  call and return `output_url` set.
- `get_job` — collect a job that `enhance_image` returned as `queued` or `running`.

The tool sends still images. What it accepts, and how large, is in its own description.

## Before the first call: connect the account

The tools run on a CraterView account, and the connection is made once per assistant.

1. The user needs a signed-in CraterView account. Sign in at https://craterview.ai — a
   guest workspace will not do, because a guest account lives only in that browser and an
   assistant linked to it could never be found or revoked again.
2. When this plugin's server is first used, the client opens CraterView's authorization
   page, which asks for a **code**, not a key. In the CraterView dashboard
   (https://craterview.ai/dashboard) the user opens **Connect an assistant**, presses
   **Get a code**, and pastes it. A code lasts ten minutes and works once.
3. From then on every job runs on that account, is billed to it, and appears in its history.

If a tool call fails saying the connection's key was revoked or replaced, the user rotated
their API key or the account was suspended. Reconnect — a fresh code from the dashboard —
rather than retrying.

In Claude Code, `/mcp` shows the server's status and starts the authorization if it has not
happened. Any other MCP client connects the same way by adding
`https://mcp.craterview.ai/mcp` as a server.

**No API key is involved in this path**, and that is the point of it: nothing worth stealing
is ever on screen. If the user wants a key — to call the API from their own code, a script or
a server — it is on the same dashboard under **Developer API**: **Show my API key**, then
**Copy**. Keys begin with `cv_`. Rotating one replaces it immediately and disconnects every
assistant linked to the account. The `craterview-api` plugin is the skill for that route.

## Choosing the model

`enhance_image` takes a `model` and a `params` object. **Both are read from CraterView's
catalog each time the server is used**, so the tool's own description is the current list:
every model it offers, what each is for, and every setting each one takes with its range and
default. Read it there rather than from memory; models and settings are added without this
skill changing.

- Match the request to what each model says it is for. When the request is ambiguous, ask
  before you guess: "sharpen this old photo" is an enhancement if the print is intact and a
  repair if it is torn or stained, and enlarging a damaged print sharpens the damage.
- **Send only the settings the user asked for.** Each has a default, and each model refuses a
  setting it does not take — the tool says which models take each one.
- **Leave an enlargement factor out** unless the user named one. A model that enlarges bounds
  the size of what it returns, so the largest factor a picture can have depends on how large
  it is, and omitting it takes the largest that fits. A factor too large for the picture is
  refused rather than quietly reduced; naming a region of the picture is how its subject gets
  the full factor.
- If a model's description warns about something — a face rebuilt as a likeness rather than
  the original pixels, say — pass that on to the user before they rely on the result.

## Handing over the image

Give exactly one of `image_url` or `image_base64`.

- **Prefer `image_url`** whenever the image has an address. Inline base64 is text in your
  context; a one-megabyte image is hundreds of thousands of tokens.
- Use `image_base64` only for an image already in the conversation with no URL. A `data:`
  URL is accepted.
- The user's local file has no URL. If the client can read files but not serve them, base64
  is the route; say so, and prefer the smallest copy that will do.

## Reading the result

Every result is a link, never the bytes.

- `status: succeeded` — `output_url` displays in a page, `download_url` saves under the
  job's name. **Both are presigned and expire.** Give the user the link now, or fetch the
  image if it is wanted for something further; do not store the URL as if it were permanent.
  `get_job` mints fresh links.
- `status: queued` or `running` — the job is still going. `eta_seconds` is the platform's
  estimate of the time left, and the note says it in words: tell the user how long to expect.
  Then call `get_job` with the `job_id`. It waits for the job before it answers, so if it
  answers that the job is still going, call it again straight away; there is no need to pause
  between calls. Each answer carries the current estimate. **Do not call
  `enhance_image` again**: that runs and charges the work twice.
- `scale` — the enlargement factor the job was sent. When you left it out, `scale_note` says
  so and how the one used was chosen: the largest factor whose result fits the account's size
  limit for that model. Tell the user which factor was used. At 1× the picture keeps its size
  and has its detail rebuilt; if they wanted it larger, name a region of the picture (`roi`),
  which is what comes back enlarged.
- `status: failed` — `error` says what happened in words the user can act on. A failed job
  is not charged.

## Cost

Every completed job charges the account's credits — the price is per model and stated by the
catalog. An account with no credit still runs: the job goes to the community queue, which is
served after paid work, so it waits longer rather than being refused. Say what a batch will
cost before running one, and never re-run a job the user did not ask to repeat.
