# craterview

Python client for the [CraterView](https://craterview.ai) image enhancement and
restoration API.

```bash
pip install craterview
```

Python 3.9 or newer. `requests` is the only dependency.

## Quickstart

```python
from craterview import CraterView

cv = CraterView(api_key="cv_...")
result = cv.run("photo.jpg", style="photo")
result.save("photo-4x.jpg")
```

`run()` collapses the upload → submit → poll sequence into one call. That sequence is the
API's shape rather than an accident, but it is not something every caller should have to
reimplement.

## An API key

Keys begin with `cv_` and are issued from your dashboard. Pass one to the constructor, or
leave it out and the client talks to the API unauthenticated — useful only against a local
development gateway.

```python
cv = CraterView(api_key=os.environ["CRATERVIEW_API_KEY"])
```

A key carries your whole allowance and does not expire, so keep it server-side. It is a
credential for programs; it is not a login.

## The one call

```python
job = cv.run(
    image,                       # path, str, bytes, or a file object
    model="cv-restore-v1",        # default
    wait=30.0,                   # seconds to hold the connection open
    timeout=600,                 # total seconds before giving up
    raise_on_failure=True,
    **params,                    # model parameters, e.g. style="photo"
)
```

It waits server-side first, so a job that finishes quickly costs a single round trip, then
falls back to polling for anything slower. With `raise_on_failure=False` a failed job is
returned rather than raised, and `job.error` says why.

Which parameters a model accepts is published by the API rather than hard-coded here — see
`cv.models()`.

## The explicit path

Useful when you want to hold the key, submit later, or fan out.

```python
input_key = cv.upload("photo.jpg")                       # → "inputs/..."
job = cv.submit("cv-restore-v1", input_key, style="photo")
job = cv.wait_for(job, timeout=600)
data = job.bytes()
```

Image bytes never pass through the API. `upload()` PUTs them straight to object storage on
a presigned URL and `bytes()` GETs the result the same way; the API moves keys, not pixels.

## Retries are yours to write

**This client does not retry.** That is deliberate: only the caller knows where one logical
request ends and the next begins, and a retry the client invented would be a second job you
are billed for.

What it gives you instead is a key generator. Make one key per *logical request* and reuse
it for every attempt at that request:

```python
from craterview import new_idempotency_key

key = new_idempotency_key()                # once, before the first attempt
for attempt in range(3):
    try:
        job = cv.submit("cv-restore-v1", input_key, idempotency_key=key, style="photo")
        break
    except (ConnectionError, TimeoutError):
        continue                           # same key, so at most one job is ever created
```

Generating a fresh key per attempt defeats the point entirely — the server has nothing to
match against and every attempt starts its own job. Reusing a key with a *different* body
is rejected with 409 rather than quietly handing back the earlier result. Claims are kept
for 24 hours; past that the same key starts new work.

## Errors

Everything raised by this client descends from `CraterViewError`, so one `except` catches
the lot. `.status` carries the HTTP status where there was one.

| Exception | Meaning |
|---|---|
| `RateLimited` | 429. `.retry_after` is seconds until the window rolls over. |
| `JobFailed` | The job ran and did not succeed. `.args[0]` says what you can do about it; `.error_code` is the half to branch on. |
| `CraterViewError` | Everything else, including 4xx and 5xx from the API. |

## `Job`

Every field the API publishes on a job is exposed here.

| | |
|---|---|
| `id`, `model`, `status` | `status` is one of `queued`, `running`, `succeeded`, `failed` |
| `created_at` | Timezone-aware `datetime` |
| `succeeded`, `done` | `done` covers both terminal states |
| `error` | Set when the job failed. Safe to show a user |
| `error_code` | The same fact, as a stable identifier. Branch on this, show the other |
| `credits` | **What you were billed** |
| `eta_seconds` | The estimate made at submit. Absent once the job has settled |
| `community` | True when the job is on the community queue: served after priority work, taking a small share of it |
| `output_url`, `download_url` | The result, presigned. One to display, one to save |
| `thumb_url` | A small JPEG of the result, for listings. Null when none was drawn |
| `input_url` | The file you sent. Null once it has expired — inputs go after a day |
| `output_content_type` | The result's media type |
| `output_bytes` | The result's size in bytes |
| `bytes()`, `save(path)` | Download the result |

`result` is the whole of what the job produced, and the five rows above it that describe the
file are accessors onto `result["output"]` rather than separate fields — the API states those
links once. A model with no file to hand back returns its answer in `result` and leaves every
one of them null.

`credits` is the only figure about cost the API states, and the price is fixed and published
per model, so an invoice reconciles against `credits` alone. For how long a job took, subtract
`created_at` from `finished_at` — that is the wait you had, queue included.

**Uploads declare their size.** `upload()` and `run()` measure the bytes they are about
to send and declare that figure; the API signs it into the presigned URL, so storage accepts
exactly that length and nothing else. You do not pass it — it is read off the data in hand,
which is what makes it impossible to get wrong.

**Running out of credit does not stop you.** A job submitted against a balance of zero is
accepted, charged and run — it simply waits in the community queue for capacity that paid
work is not using, and comes back with `community` set. There is no payment error to
handle: credit buys a place at the front of the queue rather than the right to submit.
`eta_seconds` covers the whole wait, queue time included, so a community job simply
reports a longer one.

`output_url` and `download_url` are the same object signed two ways; the content
disposition is signed in, so the second cannot be derived from the first. Both expire, so
fetch the result rather than storing the link.

`thumb_url` and `input_url` are for building a job listing: a few-hundred-pixel preview so a
page of results costs kilobytes, and the original so a result can be shown against what made
it. They keep very different company on expiry — the preview lives as long as the result,
while inputs are deleted a day in — so treat a missing `input_url` as normal rather than as
an error.

## Webhooks

Pass `webhook_url` on submit — an absolute `http` or `https` URL, refused at submit if it is
not one — and we `POST` to it once the job settles. Verify every delivery before you act on
it: anyone who learns your URL can send you a plausible-looking body.

```python
from craterview import CraterView, verify_webhook, InvalidSignature

secret = CraterView(api_key="cv_...").webhook_secret()   # created the first time you ask

@app.post("/hooks/craterview")
def hook(request):
    try:
        event = verify_webhook(request.body, request.headers, secret)
    except InvalidSignature:
        return 400                       # not from us; do nothing at all

    # Answer now and work afterwards. A slow endpoint is retried while it is still
    # working, so treat deliveries as at-least-once and make this idempotent on `id`.
    enqueue(event["id"])
    return 200
```

Every copy of a delivery states the same thing, so the first one you accept is the whole
answer — later copies of an `id` you have already handled can be dropped rather than
reconciled.

**Pass the raw bytes.** Most frameworks parse JSON for you, and re-serialising it changes the
bytes the signature was computed over. In Flask that is `request.get_data()`, in Django
`request.body`, in FastAPI `await request.body()`.

The body carries the same fields a job read does, by the same names — including `result` and
the links inside it. The one difference is what happens to a field that does not apply: a
callback states every key and sets it `null`, where a job read leaves it out. So a subscriber
parses one shape and never has to ask which keys arrived.

The links expire like any others. A delivery normally arrives within seconds of the job
settling and they are good for an hour, but a subscriber that queues work for later should
read the job again rather than storing them.

### Rotating the secret

```python
new = cv.rotate_webhook_secret()
```

The old secret keeps being sent alongside the new one for a day, and `verify_webhook` accepts
either — so deploy the new one whenever suits rather than the moment you asked for it.

## Managing keys

```python
cv.keys()                    # every key on the account, by prefix; revoked ones stay listed
cv.create_key("staging")     # the value is in this response and nowhere else
cv.revoke_key("key_...")     # immediate, and not reversible
```

Several keys on one account is the ordinary arrangement — one per service or environment, so
revoking one leaves the others working. They share the account's credits and history.

To rotate without downtime: create the new key, move your clients onto it, then revoke the
old one. You cannot revoke the key you are calling with.

## Everything else

```python
cv.models()                       # models, their parameter schemas, and queue depth
cv.jobs(limit=50, status="succeeded")   # your history, newest first, paging transparently
cv.job("job_...")                 # one job by id
cv.usage()                        # credit balance, spend and job counts
```

`jobs()` is a generator and pages for you.

## Configuration

```python
CraterView(
    api_key=None,
    base_url="https://api.craterview.ai",
    timeout=60.0,                 # per-request, seconds
)
```

`base_url` is what you change to point at a local gateway.

## Versioning

Semver, from `0.1.0` on. A release that removes a public name, renames an attribute on `Job`
or changes the type of one, gets a minor bump while this is `0.x` and a major bump after
`1.0.0`; anything purely additive gets a patch. Pin what you depend on.

The API this wraps adds fields to its responses without warning, so treat an unfamiliar key in
`result` or a `status` you do not recognise as something to ignore rather than to fail on.

## License

Apache-2.0. See [LICENSE](LICENSE).
