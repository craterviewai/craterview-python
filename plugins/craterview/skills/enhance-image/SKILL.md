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

Stills only. Video is not yet in service.

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

`enhance_image` takes a `model`. The catalog the API serves is authoritative; this is what
was in service when this skill was written.

| The user wants | Model | Notes |
| --- | --- | --- |
| Sharper, larger, less noisy, fewer compression artifacts — a soft scan, a small or cropped photo, a screenshot | `cv-enhance-v3` (default) | `scale` 1–4, default 4. Use a smaller factor when the source is already large or only detail is wanted. |
| A damaged print repaired — tears, creases, scratches, dust, faded colour | `cv-restore-v1` | Returns about one megapixel whatever was sent: it repairs, it does not enlarge. Takes no `scale`. It rebuilds a face as a close likeness rather than the original pixels, so tell the user to keep the source. |

Ask before you guess when the request is ambiguous: "sharpen this old photo" is an
enhancement if the print is intact and a restoration if it is torn or stained. Enlarging a
damaged print sharpens the damage.

Each model refuses a parameter it does not publish, so send only what the chosen model
declares — a `scale` on `cv-restore-v1` is an error, not a no-op.

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
- `status: queued` or `running` — the job is still going. Call `get_job` with the `job_id`
  after a few seconds. **Do not call `enhance_image` again**: that runs and charges the work
  twice. A large image takes tens of seconds; a tight loop only spends the account's rate
  limit.
- `status: failed` — `error` says what happened in words the user can act on. A failed job
  is not charged.

## Cost

Every completed job charges the account's credits — the price is per model and stated by the
catalog. An account with no credit still runs: the job goes to the community queue, which is
served after paid work, so it waits longer rather than being refused. Say what a batch will
cost before running one, and never re-run a job the user did not ask to repeat.
