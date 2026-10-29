# Upload, submit and wait as separate steps

When the caller wants to hold the key, submit later, fan out, or not block:

```python
input_key = cv.upload("photo.jpg")                       # → "inputs/..."
job = cv.submit("cv-enhance-v3", input_key, scale=4)     # 201, status queued
job = cv.wait_for(job, timeout=600)                       # or: cv.job(job.id) yourself
data = job.bytes()
```

## Retries: outside `run()`, the caller's to do right

Each client's `run()` asks its submission once more under one idempotency key, and its wait
reads on through a failure that got no answer; nothing else retries, because only the caller
knows where one logical request ends. Around `submit()`, make one idempotency key per logical
request and reuse it for every attempt:

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
different body is a 409. A `RateLimited` (429) carries `retry_after` in seconds. A job that
ran and failed is not this loop's to repeat: only one whose `error_code` is
`inference_failed` is worth submitting again as it was.
