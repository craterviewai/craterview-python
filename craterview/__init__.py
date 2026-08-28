"""CraterView.ai Python client.

    from craterview import CraterView

    cv = CraterView(api_key="cv_...")
    result = cv.run("photo.jpg", style="photo")
    result.save("photo-4x.jpg")

The three-call upload/submit/poll dance is the API's shape, not something a caller should
have to reimplement, so the client collapses it into one method and streams the bytes for
you.

**This file is published**, to GitHub and PyPI, and is read by people who have this package
and nothing else. Write for them: what the client does and what a caller has to know, never
how the service behind it is built.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, fields as _dataclass_fields
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _installed_version
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import requests

__all__ = ["CraterView", "Job", "CraterViewError", "RateLimited", "JobFailed",
           "new_idempotency_key", "verify_webhook", "InvalidSignature",
           "SIGNATURE_HEADER", "TIMESTAMP_HEADER"]

# `version` in pyproject.toml is the only place a release number is written; this reads it
# back from the installed distribution. It goes out as the User-Agent below, so a second
# copy here could drift and have the client announce a version it is not — which is worse
# than no version at all, because a server log would be actively misleading.
try:
    __version__ = _installed_version("craterview")
except PackageNotFoundError:
    # Imported from a source checkout that was never installed, which is how the contract
    # suite runs it. Deliberately not the real number: a checkout is not a release, and
    # saying so in the User-Agent is the useful thing to report.
    __version__ = "0.0.0+dev"

DEFAULT_BASE_URL = "https://api.craterview.ai"
# The server rejects a longer wait outright, so asking for one costs a 422 rather than a
# longer wait. Keep in step with MAX_WAIT_SECONDS in the gateway.
MAX_SERVER_WAIT = 30.0


def new_idempotency_key() -> str:
    """A random key for safely repeating a submission.

    Generate one per *logical request* and reuse it for every attempt at that request:

        key = new_idempotency_key()          # once, before the first attempt
        for attempt in range(3):
            try:
                job = cv.submit("cv-restore-v1", input_key, idempotency_key=key,
                                style="photo")
                break
            except (ConnectionError, TimeoutError):
                continue                      # same key, so at most one job is created

    Generating a fresh key per attempt defeats the purpose entirely — the server has
    nothing to match against and each attempt starts its own job. That is the mistake this
    function exists to make harder, not easier: it is deliberately not called for you,
    because only the caller knows where one request ends and the next begins.

    Reusing a key with a *different* request body is rejected with 409 rather than
    silently returning the earlier result.

    This client does not retry on your behalf. The loop above is yours to write.
    """
    return f"idem_{secrets.token_urlsafe(24)}"


class CraterViewError(RuntimeError):
    """Base class, so callers can catch everything from this client with one except."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(CraterViewError):
    """429 — too fast, or too much at once. Two different limits answer with this.

    One is the key's request rate. The other is the account's cap on jobs queued or
    running at the same time, which exists so one caller cannot occupy the whole fleet.

    `retry_after` is seconds to wait, and how good a number it is depends on which limit
    you hit: exact for the rate limit, where it is when the window rolls over, and a hint
    for the in-flight cap, where a slot frees when one of your own jobs finishes and the
    server can only quote the model's typical duration. `usage()` reports the cap and what
    you currently hold against it.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, 429)
        self.retry_after = retry_after


class JobFailed(CraterViewError):
    """The job ran and did not succeed.

    `error` says what the caller can do about it and `error_code` is the half to branch on,
    because the prose is written for a person and gets reworded. `inference_failed` means the
    fault was ours, the credits were refunded, and the same call is worth making again.

    An unfamiliar code is a failure with no special handling, not an error in itself: new ones
    appear as new things become worth telling apart.
    """

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


SIGNATURE_HEADER = "CV-Signature"
TIMESTAMP_HEADER = "CV-Timestamp"

# How far out of date a delivery's timestamp may be before `verify_webhook` refuses it.
# Generous enough to cover a delivery that was retried before it reached you, tight enough
# that a copy captured earlier is no longer accepted.
DEFAULT_TOLERANCE_SECONDS = 300.0


class InvalidSignature(CraterViewError):
    """A delivery did not verify. Do not act on the body.

    Raised for every reason a delivery can fail to check out — a wrong signature, a missing
    header, a timestamp too old — because from a receiver's point of view they are one
    outcome: this is not something we sent, so it does not get to do anything.
    """


def verify_webhook(body: bytes, headers: "dict[str, str] | Any", secret: str,
                   *, tolerance: float = DEFAULT_TOLERANCE_SECONDS) -> dict:
    """Check that a callback really came from CraterView, and return its parsed body.

        @app.post("/hooks/craterview")
        def hook(request):
            event = verify_webhook(request.body, request.headers, MY_SECRET)
            ...

    Raises `InvalidSignature` if it does not check out. **Verify before you parse**, and
    pass the raw bytes exactly as received: re-serialising the JSON changes them, and the
    signature is over what was sent rather than over what your framework made of it.

    `headers` may be a plain dict or any mapping with case-insensitive `get`, which is what
    most frameworks hand you. Header names are matched either way.

    Get your secret from `CraterView.webhook_secret()`, or from the dashboard.
    """
    import hashlib
    import hmac
    import json

    signature = _header(headers, SIGNATURE_HEADER)
    timestamp = _header(headers, TIMESTAMP_HEADER)
    if not signature or not timestamp:
        raise InvalidSignature("delivery is missing its signature headers")

    try:
        age = time.time() - int(timestamp)
    except ValueError:
        raise InvalidSignature("delivery has an unreadable timestamp") from None
    # Both directions. A clock ahead of ours is as much a reason to distrust a delivery as
    # one behind it, and only checking the past accepts anything dated next year.
    if tolerance and abs(age) > tolerance:
        raise InvalidSignature(f"delivery is {age:.0f}s out of date")

    expected = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    # A list, because a rotated secret keeps signing alongside its replacement for a while —
    # so accept the delivery if any entry matches, and you can deploy a new secret whenever
    # suits rather than the moment you asked for one.
    #
    # `compare_digest` rather than `==`: a comparison that returns early leaks, one character
    # at a time, how much of a guess was right.
    if not any(hmac.compare_digest(expected, entry.strip()) for entry in signature.split(",")):
        raise InvalidSignature("signature does not match")

    try:
        return json.loads(body)
    except ValueError:
        raise InvalidSignature("delivery verified but its body is not JSON") from None


def _header(headers: Any, name: str) -> str | None:
    """One header, however the caller's framework spells its container."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    found = getter(name)
    if found is not None:
        return found
    # A plain dict is case-sensitive, and a framework that lower-cases its headers is
    # ordinary rather than unusual.
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    return lowered.get(name.lower())


def _timestamp(value: Any) -> datetime | None:
    """Parse a wire timestamp, or give up quietly.

    The API sends RFC 3339 ending in `Z`, which `fromisoformat` only accepts from Python
    3.11 — this package supports 3.9. Hence the substitution rather than a direct parse.

    An unparseable value becomes None instead of raising. A caller is holding this object
    to get at a result; failing the whole response over a timestamp they may never read
    would be the wrong trade.
    """
    if not isinstance(value, str):
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class Job:
    """A submitted job. Mirrors the API's job representation field for field.

    `credits` is what you were billed, and it is the only figure about cost the API states.
    For how long a job took, `finished_at` minus `created_at` is the wait you actually had,
    queue included — both are here and both are on the webhook, which states the same fields
    by the same names.

    `result` is the whole of what the job produced. Where the model wrote a file,
    `result["output"]` carries its links, and `output_url` / `download_url` / `thumb_url` /
    `output_content_type` / `output_bytes` are accessors onto that — the same object signed
    two ways, one to display and one to hand a person as a file. The disposition is signed
    in, so the second cannot be derived from the first without the storage credential. Both
    are presigned and expire: fetch the result rather than storing the link.

    `eta_seconds` is the whole of what is reported about waiting: how long until the result,
    counting time spent waiting for a GPU as well as time spent on one. An estimate and never
    a promise — read it as guidance, not a deadline. Absent once a job has settled.

    `thumb_url` is a small JPEG of the result, for showing a page of jobs without
    downloading a page of full-size outputs. `input_url` is the file you sent, so a result
    can be shown against what it was made from. The two expire on very different clocks:
    the preview goes with the result, while inputs are deleted after a day — much sooner
    than the output — so `input_url` is null for most of a job's life and code that reads
    it should expect nothing there.

    `community` says the job was submitted against a balance of zero and is on the queue
    served after priority work, which takes a small share of it rather than only what is
    left. Nothing is refused for want of credit — credit buys a place at the front of the
    queue, not the right to submit — so an empty balance means a longer wait and never an
    error.
    """

    id: str
    model: str
    status: str
    # Whatever you passed as `custom_id` at submit, handed straight back. We store it and
    # echo it; nothing here interprets it, and jobs cannot be looked up by it.
    custom_id: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    # The stable half of `error` — branch on this rather than on the sentence, which is
    # written for a person and may be reworded. `inference_failed` means the platform
    # declined to explain the failure, and a retry is worth trying.
    error_code: str | None = None
    # Everything this job produced, in one object: the model's own answer, plus an `output`
    # section carrying the file's links when the model produced one. Some models — a
    # detector, say — return only the answer and no file at all, so read this rather than
    # assuming there is something to download.
    #
    # `output_url` and the three beside it are **properties** below rather than fields: the
    # wire states each of those facts once, inside `result.output`, and reading them out of
    # it is a client library's job — `job.output_url` is nicer than reaching into a dict, and
    # it is how every example in this file is written.
    result: dict | None = None
    input_url: str | None = None
    # Whole credits. A credit is not divisible, and a partial video second rounds up at the
    # charge rather than arriving as a fraction.
    credits: int | None = None
    eta_seconds: float | None = None
    community: bool = False
    # Whether an automated check thought this image may fall outside what the service
    # allows. `None` means it was not checked.
    flagged: bool | None = None
    # Whether this result is retained past the ordinary expiry, because its owner asked.
    # A kept result also keeps working links.
    kept: bool = False
    _client: "CraterView | None" = None

    @property
    def _output(self) -> dict:
        """The `output` section of the result, or an empty dict.

        Empty for a job that has not finished, and for one whose model produced no file at
        all — a detector's answer is entirely `result` fields. Both read the same here, which
        is why the accessors below return None rather than raising: "no file yet" and "never
        going to be a file" are the caller's distinction to draw from `status`, not ours.
        """
        return ((self.result or {}).get("output")) or {}

    @property
    def output_url(self) -> str | None:
        """The result, signed to display. Presigned and short-lived — fetch, do not store."""
        return self._output.get("url")

    @property
    def download_url(self) -> str | None:
        """The same object signed to save rather than display.

        Not derivable from `output_url`: the content disposition is signed into the URL, so
        re-signing needs the storage credential, which is why the server sends both.
        """
        return self._output.get("download_url")

    @property
    def thumb_url(self) -> str | None:
        """A small JPEG of the result, for showing a page of jobs without downloading a page
        of full-size outputs. Absent where the worker could not draw one."""
        return self._output.get("thumbnail_url")

    @property
    def output_content_type(self) -> str | None:
        return self._output.get("content_type")

    @property
    def output_bytes(self) -> int | None:
        """The result's size. Reachable before this only by reading `result` by hand."""
        return self._output.get("bytes")

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed")

    def bytes(self) -> bytes:
        """Download the result."""
        if not self.output_url:
            raise CraterViewError(f"job {self.id} has no result (status {self.status})")
        resp = requests.get(self.output_url, timeout=300)
        resp.raise_for_status()
        return resp.content

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_bytes(self.bytes())
        return path

    @classmethod
    def _from(cls, payload: dict, client: "CraterView") -> "Job":
        # Driven off the dataclass rather than a second hand-written tuple of names. The
        # list here and the fields above drifted apart once already, and the symptom was
        # silent: the server sent `credits` and the client dropped it, so a caller could
        # not see what a job had cost. Adding a field above is now the only edit needed.
        #
        # A key the server omitted is left out rather than passed as None, so each field
        # falls back to the default declared above. That matters for the first field whose
        # default is not None: `community` is False, and an older gateway that does not
        # send it must leave it False rather than making it null.
        known = {f.name: payload[f.name] for f in _dataclass_fields(cls)
                 if not f.name.startswith("_") and f.name in payload}
        # Which fields are times is read off their annotations, for the same reason the
        # names above are: naming them here is the second list, and the second list is what
        # drifts. `from __future__ import annotations` makes these strings, which is why
        # this matches text rather than the type.
        for field in _dataclass_fields(cls):
            if "datetime" in str(field.type) and field.name in known:
                known[field.name] = _timestamp(known[field.name])
        return cls(**known, _client=client)


class CraterView:
    def __init__(self, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._session.headers["User-Agent"] = f"craterview-python/{__version__}"

    # ------------------------------------------------------------------ internals

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self._session.request(method, f"{self.base_url}{path}",
                                     timeout=self.timeout, **kwargs)
        if resp.status_code == 429:
            raise RateLimited(self._detail(resp),
                              retry_after=float(resp.headers.get("Retry-After", 0) or 0))
        if resp.status_code >= 400:
            raise CraterViewError(self._detail(resp), resp.status_code)
        # A 204 carries no body, so asking for JSON raises on a call that succeeded. Checked
        # by status rather than by looking for an empty body: this is the only response the
        # API sends without one, and a status is a fact rather than an inference.
        if resp.status_code == 204:
            return None
        return resp.json()

    @staticmethod
    def _detail(resp: requests.Response) -> str:
        try:
            return resp.json().get("detail") or resp.text
        except Exception:
            return resp.text or f"HTTP {resp.status_code}"

    # ---------------------------------------------------------------------- public

    def models(self) -> list[dict]:
        """Available models: parameter schemas, prices, limits, and your queue.

        Prices are published here, so the cost of a job is knowable before submitting it.

        Needs a key, and not only because the figures are live: `queue_depth` and
        `community` are answered *for the queue your key would use*. The API looks up the
        account's balance and reports the queue a job from this key would land in — an
        account in credit gets the priority queue, an account at zero the community one,
        which is served after priority work and takes a small share of it. So two keys asking
        at the same moment can get different numbers, and buying credit changes yours.

        `community` here means what `Job.community` means on a submitted job, and
        `queue_depth` is how much work is ahead of you on that queue before you submit —
        the counterpart to `Job.eta_seconds` once you have.

        A model that is available is not always listed — a model in trial, or being
        retired, stays usable by name while absent from this catalogue.

        And a model that is listed is not always usable: `coming_soon` marks one that is
        announced but not yet in service, and submitting to it raises with 409 until it
        launches. Everything else published about it is final, so an integration can be
        written against it in advance. `video_coming_soon` says the same of one model's
        video, which is why `accepts` names no clip for it yet.
        """
        return self._request("GET", "/v1/models")

    def upload(self, image: str | Path | bytes | BinaryIO,
               content_type: str | None = None) -> str:
        """Put an image in storage and return its key.

        Bytes go straight to object storage on a presigned URL, never through the API.
        """
        if isinstance(image, (str, Path)):
            path = Path(image)
            data = path.read_bytes()
            content_type = content_type or _guess_type(path)
        elif isinstance(image, bytes):
            data = image
            content_type = content_type or "image/png"
        else:
            data = image.read()
            content_type = content_type or "image/png"

        # The length is declared and then signed into the URL, so it must be the length
        # actually sent — storage refuses any other size with a signature error. `data` is
        # bytes by this point either way, so it is measured rather than guessed.
        slot = self._request("POST", "/v1/uploads",
                             json={"content_type": content_type, "content_length": len(data)})
        put = requests.put(slot["upload_url"], data=data,
                           headers={"Content-Type": content_type}, timeout=300)
        put.raise_for_status()
        return slot["input_key"]

    def submit(self, model: str, input_key: str, *, wait: float = 0,
               idempotency_key: str | None = None, webhook_url: str | None = None,
               custom_id: str | None = None, **params) -> Job:
        """Queue a job. `wait` holds the response open for a settled result.

        Pass `idempotency_key` — see `new_idempotency_key` — if you intend to retry this
        submission. Without one, a repeat after a failed or uncertain request starts a
        second job and is billed for both.

        `custom_id` is your own name for this job, returned on every read of it and on the
        webhook. It is named here rather than left to `**params` deliberately: a parameter is
        checked against the model's schema and would be refused, and this is not a parameter.

        `webhook_url` is where the signed callback goes once the job settles. It must be an
        absolute http or https URL — anything else is refused here, rather than becoming a
        delivery that fails somewhere you cannot see.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        body = {"model": model, "input_key": input_key, "params": params}
        if webhook_url:
            body["webhook_url"] = webhook_url
        if custom_id:
            body["custom_id"] = custom_id
        payload = self._request("POST", f"/v1/jobs?wait={wait}", json=body, headers=headers)
        return Job._from(payload, self)

    def job(self, job_id: str) -> Job:
        return Job._from(self._request("GET", f"/v1/jobs/{job_id}"), self)

    def jobs(self, limit: int = 50, status: str | None = None) -> Iterator[Job]:
        """Iterate the account's job history, newest first, paging transparently.

        Scoped to the account rather than to this key, so a key sees every job the account
        has run and not only the ones it submitted itself.
        """
        before = None
        while True:
            query = f"/v1/jobs?limit={min(limit, 200)}"
            if status:
                query += f"&status={status}"
            if before:
                query += f"&before={before}"
            page = self._request("GET", query)
            for item in page["data"]:
                yield Job._from(item, self)
            if not page.get("has_more"):
                return
            cursor = page.get("next_before")
            # Stop on a missing or non-advancing cursor. Either would otherwise spin
            # forever inside the caller's app, hammering the API with no error to show.
            if not cursor or cursor == before:
                return
            before = cursor

    def usage(self) -> dict:
        """The account's credit balance, what it has spent, and what is in flight.

        All-time, not monthly: credits are granted and deplete rather than renewing.
        Counts committed work rather than completed, so a queued job is already in the
        figures — otherwise this and the balance a submission is checked against would
        disagree.

        `credits_remaining` is always a number, floored at zero. `jobs_in_flight` and
        `max_jobs_in_flight` are the state of the queue and are what a 429 on submit is
        about.

        Account-wide, so every key on an account reports the same figures.
        """
        return self._request("GET", "/v1/usage")

    def webhook_secret(self) -> str:
        """The secret your callbacks are signed with, created the first time you ask.

        Pass it to `verify_webhook`. Unlike an API key this can be read back whenever you
        need it — but treat it as a credential all the same: anyone holding it can produce a
        delivery your receiver will accept as genuine.
        """
        return self._request("GET", "/v1/webhooks/secret")["secret"]

    def rotate_webhook_secret(self) -> str:
        """Replace the signing secret and return the new one.

        The secret you were using keeps being sent alongside it for a window, so deliveries
        in that period carry a signature under each and `verify_webhook` accepts either.
        Deploy the new one whenever suits; nothing fails in between.

        Rotating twice inside one window drops the older of the two.
        """
        return self._request("POST", "/v1/webhooks/secret/rotate")["secret"]

    def keys(self) -> list[dict]:
        """Every key on the account, including revoked ones, by prefix rather than value.

        A revoked key stays listed so that a client which started failing can be explained.
        """
        return self._request("GET", "/v1/keys")

    def create_key(self, name: str = "api") -> dict:
        """Mint another key. **The value is in this response and nowhere else.**

        Keys are stored as hashes, so one that is not written down has to be replaced rather
        than recovered. Several keys on an account is the ordinary arrangement — one per
        service or environment — and they share the account's credits and history.

        This is also how you rotate without downtime: create the new key, move your clients
        onto it, then revoke the old one.
        """
        return self._request("POST", "/v1/keys", json={"name": name})

    def revoke_key(self, key_id: str) -> None:
        """Stop a key working. Immediate, and not reversible.

        You cannot revoke the key this client is authenticating with — the call would
        succeed and leave you unable to make another.
        """
        self._request("DELETE", f"/v1/keys/{key_id}")

    def wait_for(self, job: Job | str, timeout: float = 600, poll: float = 1.0) -> Job:
        """Block until a job settles. Prefer `wait=` on submit for short jobs."""
        job_id = job.id if isinstance(job, Job) else job
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.job(job_id)
            if current.done:
                return current
            time.sleep(poll)
        raise CraterViewError(f"job {job_id} did not finish within {timeout}s")

    def run(self, image: str | Path | bytes | BinaryIO, *, model: str = "cv-restore-v1",
                wait: float = MAX_SERVER_WAIT, timeout: float = 600,
                raise_on_failure: bool = True, **params) -> Job:
        """Upload, submit and wait, in one call — the common case.

        Uses server-side waiting first so a fast job costs a single round trip, then falls
        back to polling for anything slower.
        """
        input_key = self.upload(image)
        job = self.submit(model, input_key, wait=min(wait, MAX_SERVER_WAIT), **params)
        if not job.done:
            job = self.wait_for(job, timeout=timeout)
        if raise_on_failure and not job.succeeded:
            raise JobFailed(job.error or "job failed", job.error_code)
        return job


def _guess_type(path: Path) -> str:
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
