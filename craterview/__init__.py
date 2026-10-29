"""CraterView.ai Python client.

    from craterview import CraterView

    cv = CraterView(api_key="cv_...")
    result = cv.run("photo.jpg", scale=4)
    result.save("photo-4x.jpg")

The three-call upload/submit/poll dance is the API's shape, not something a caller should
have to reimplement, so the client collapses it into one method and streams the bytes for
you.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, fields as _dataclass_fields
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _installed_version
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import urlsplit

import requests

__all__ = ["CraterView", "Job", "Upload", "CraterViewError", "RateLimited", "JobFailed",
           "ConnectionFailed", "TimedOut", "InvalidId", "new_idempotency_key", "verify_webhook",
           "InvalidSignature", "SIGNATURE_HEADER", "TIMESTAMP_HEADER"]

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
# The server rejects a longer wait outright, so asking for one costs a 422 rather than the
# wait you asked for. `run()` clamps to this rather than letting that happen.
MAX_SERVER_WAIT = 30.0
# The largest page the job history serves. Same reasoning: a bigger `limit` is refused, so
# `jobs()` clamps rather than spending a request to be told no.
MAX_PAGE = 200

# The shape of each kind of id the API names in a path, exactly as it issues them. A method
# that puts an id in its path takes one of these and nothing else, because anything else
# builds some other request than the one the method names: `..` is a dot segment that the
# transport resolves before sending, so `revoke_key("../account")` would be a request to
# close the account, and quoting does not help, since `%2e%2e` is resolved the same way.
_IDS = {"job": re.compile(r"job_[0-9a-f]{24}"), "key": re.compile(r"key_[0-9a-f]{24}")}


def new_idempotency_key() -> str:
    """A random key for safely repeating a submission.

    Generate one per *logical request* and reuse it for every attempt at that request:

        key = new_idempotency_key()          # once, before the first attempt
        for attempt in range(3):
            try:
                job = cv.submit("cv-enhance-v3", input_key, idempotency_key=key,
                                scale=4)
                break
            except (ConnectionError, TimeoutError):
                continue                      # same key, so at most one job is created

    The two it catches are `ConnectionFailed` and `TimedOut`, this client's errors for a
    request that never got an answer; each is also the builtin the loop names. Anything
    else the API answered with — a refusal, a 409 — is not a reason to repeat the request.

    Generating a fresh key per attempt defeats the purpose entirely — the server has
    nothing to match against and each attempt starts its own job. That is the mistake this
    function exists to make harder, not easier: it is deliberately not called for you,
    because only the caller knows where one request ends and the next begins.

    Reusing a key with a *different* request body is rejected with 409 rather than
    silently returning the earlier result.

    `submit()` does not retry on your behalf: the loop above is yours to write. `run()`, which
    is one whole request from upload to result, keeps a key of its own and asks once more.
    """
    return f"idem_{secrets.token_urlsafe(24)}"


class CraterViewError(RuntimeError):
    """Base class, so callers can catch everything from this client with one except.

    `status` is the HTTP status where there was one, and `code` the API's stable name for a
    refusal: branch on `code`, not on the message, and handle one you do not know by its
    status. `trace_id` is the API's `X-Trace-Id` for
    a request it answered and refused — the id to quote when asking about it. None when the
    API never answered, or when the refusal came from storage.

    `job_id` is the job an error is about, where there is one: a job that failed, one whose
    wait ran out while it carried on, or one whose result could not be fetched. Quote it when
    asking about the job, and collect a job still going by it.
    """

    def __init__(self, message: str, status: int | None = None,
                 trace_id: str | None = None, *, job_id: str | None = None,
                 code: str | None = None):
        super().__init__(message)
        self.status = status
        self.trace_id = trace_id
        self.job_id = job_id
        self.code = code


class ConnectionFailed(CraterViewError, ConnectionError):
    """The request never reached the API or storage: refused, reset, or a name that would
    not resolve. Also the builtin `ConnectionError`, so the retry loop in
    `new_idempotency_key` catches it. Nothing was received, so repeating the request with
    the same idempotency key is safe."""


class InvalidId(CraterViewError, ValueError):
    """An id that is not one the API issued, refused before any request is made. Also the
    builtin `ValueError`. Pass the `id` the API returned — a job's from `submit()` or
    `jobs()`, a key's from `keys()` or `create_key()` — exactly as it came."""


def _issued(kind: str, value: Any, argument: str) -> str:
    """`value`, if it is a `kind` id as the API issues them; otherwise `InvalidId`."""
    if isinstance(value, str) and _IDS[kind].fullmatch(value):
        return value
    raise InvalidId(f"{argument} must be a {kind} id as the API returned it — {kind}_ and "
                    f"24 hexadecimal digits — not {value!r}; nothing was sent")


class TimedOut(CraterViewError, TimeoutError):
    """No answer came within the client's `timeout`. Also the builtin `TimeoutError`.

    The request may still have arrived and been acted on, which is exactly the case an
    idempotency key exists for: repeat a submission with the same key and the API answers
    with the job the first attempt created, if it created one, rather than a second.

    `run()` takes that remedy itself, once. Raised from it for its submission, the second
    attempt got no answer either: `run()` uploads afresh each time, so repeating it is a
    different request. The message says so, and where to look for the job either attempt may
    have made.
    """


#: How long `run()` waits before asking a submission again: the pause the web page, the phone
#: app and the assistant connector take.
_RESUBMIT_AFTER = 2.0


def _worth_asking_again(failure: CraterViewError) -> bool:
    """No answer, or a failure on the platform's side or on the way to it: asked again, the
    request may be answered. A refusal, a 4xx, is the same answer the next time."""
    return isinstance(failure, (TimedOut, ConnectionFailed)) or (failure.status or 0) >= 500


def _queued_nothing(failure: CraterViewError | None) -> bool:
    """The API's own 503 to a submission: the one status it answers before anything is
    recorded, so nothing was queued or charged. A 503 page from something on the way carries
    no trace id and says nothing about the job."""
    return failure is not None and failure.status == 503 and failure.trace_id is not None


def _sent(call, *args, **kwargs) -> requests.Response:
    """Every request this client makes, the API's and storage's, with the network's
    failures raised as this client's errors rather than the transport library's.

    One place, so no call can let a `requests` exception escape: the documented retry loop
    catches the builtins these subclass, and a request made any other way would be a
    request it silently cannot retry. A timeout is checked first, because a connect timeout
    is both a timeout and a connection error, and the loop treats them alike anyway.
    """
    try:
        return call(*args, **kwargs)
    except requests.Timeout as failure:
        raise TimedOut(str(failure)) from failure
    except requests.ConnectionError as failure:
        raise ConnectionFailed(str(failure)) from failure
    except requests.RequestException as failure:
        raise CraterViewError(str(failure)) from failure


def _code(resp: requests.Response) -> str | None:
    """The API's code for a refusal, or None for an answer that is not the API's."""
    try:
        body = resp.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


class RateLimited(CraterViewError):
    """429 — too fast, or too much at once. Five different limits answer with this, and
    `code` says which: `rate_limited`, `too_many_keys`, `too_many_uploads`, `too_many_jobs`
    or `queue_full`, in the order below.

    - The key's request rate, per minute.
    - The account's new keys, per hour — counted across all its keys, and only from
      `create_key`.
    - The account's upload URLs, per minute — counted across all its keys.
    - The account's cap on jobs queued or running at the same time, which exists so one
      caller cannot occupy the whole fleet. `usage()` reports the cap and what you
      currently hold against it.
    - The queue itself, when it is full and taking no more work. Nothing you sent is
      wrong; an account with credit or a subscription submits on a queue that fills
      separately.

    `retry_after` is seconds to wait, and what it means depends on which limit you hit:
    for the two per-minute limits it is when the window rolls over; for new keys, when the
    account may be given another; for the in-flight cap and the full queue it is an
    interval to poll on, since nothing here predicts when a slot frees.
    """

    def __init__(self, message: str, retry_after: float | None = None,
                 trace_id: str | None = None, *, code: str | None = None):
        super().__init__(message, 429, trace_id, code=code)
        self.retry_after = retry_after


class JobFailed(CraterViewError):
    """The job ran and did not succeed.

    `error` says what the caller can do about it and `error_code` is the half to branch on,
    because the prose is written for a person and gets reworded. `inference_failed` means the
    fault was ours, nothing was charged, and the same call is worth making again.

    An unfamiliar code is a failure with no special handling, not an error in itself: new ones
    appear as new things become worth telling apart. `job_id` is the job, to quote when asking
    about it.
    """

    def __init__(self, message: str, error_code: str | None = None,
                 job_id: str | None = None):
        super().__init__(message, job_id=job_id)
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
    pass the raw bytes exactly as received: re-serializing the JSON changes them, and the
    signature is over what was sent rather than over what your framework made of it.

    `headers` may be a plain dict or any mapping with case-insensitive `get`, which is what
    most frameworks hand you. Header names are matched either way.

    Get your secret from `CraterView.webhook_secret()`, or from the dashboard.
    """
    import hashlib
    import hmac
    import json

    # Before the delivery is looked at: an unset secret is this receiver's configuration, not
    # a delivery that failed to check out, and refusing every delivery as forged would hide it.
    if not secret:
        raise CraterViewError("verify_webhook was given no secret: pass the one "
                              "webhook_secret() returns, or the one on the dashboard")

    signature = _header(headers, SIGNATURE_HEADER)
    timestamp = _header(headers, TIMESTAMP_HEADER)
    if not signature or not timestamp:
        raise InvalidSignature("delivery is missing its signature headers")

    # Whole seconds in ASCII digits, as the platform writes it, and nothing else: `int()`
    # would also take whitespace, a sign, underscores and other scripts' digits, and a long
    # enough run overflows the subtraction below. Fifteen digits is past any time a clock
    # will show.
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 15):
        raise InvalidSignature("delivery has an unreadable timestamp")
    age = time.time() - int(timestamp)
    # Both directions. A clock ahead of ours is as much a reason to distrust a delivery as
    # one behind it, and only checking the past accepts anything dated next year.
    if tolerance and age > tolerance:
        raise InvalidSignature(f"delivery was signed {age:.0f}s ago, longer ago than the "
                               f"{tolerance:g}s allowed: a copy replayed late, or a clock "
                               f"here that is wrong")
    if tolerance and -age > tolerance:
        raise InvalidSignature(f"delivery is dated {-age:.0f}s ahead of this machine's clock, "
                               f"more than the {tolerance:g}s allowed: check the clock here")

    expected = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    # A list, because a rotated secret keeps signing alongside its replacement for a while —
    # so accept the delivery if any entry matches, and you can deploy a new secret whenever
    # suits rather than the moment you asked for one.
    #
    # `compare_digest` rather than `==`: a comparison that returns early leaks, one character
    # at a time, how much of a guess was right. Over bytes, because it refuses to compare a
    # `str` holding anything above U+007F, and the header is whatever the sender wrote.
    # `surrogatepass` so that even a lone surrogate encodes, and is then simply not a match.
    wanted = expected.encode()
    if not any(hmac.compare_digest(wanted, entry.strip().encode("utf-8", "surrogatepass"))
               for entry in signature.split(",")):
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
    What your balance does not cover is never collected: it is written off when credit next
    arrives or a subscription renews.
    For how long a job took, `finished_at` minus `created_at` is the wait you actually had,
    queue included — both are here and both are on the webhook, which states the same fields
    by the same names.

    `result` is the whole of what the job produced. Where the model wrote a file,
    `result["output"]` carries its links, and `output_url` / `download_url` /
    `output_content_type` / `output_bytes` are accessors onto that — the same object signed
    two ways, one to display and one to hand a person as a file. The disposition is signed
    in, so the second cannot be derived from the first without the storage credential. Both
    are presigned and expire: fetch the result rather than storing the link.

    `eta_seconds` is the whole of what is reported about waiting: how long until the result,
    counting time spent waiting for a GPU as well as time spent on one. An estimate and never
    a promise — read it as guidance, not a deadline. Absent once a job has settled.

    `progress` is how far a running job has got: `{"stage": ..., "fraction": ...}`, the stage
    one of `starting`, `processing` or `saving`, and the fraction roughly how much of the run
    is done — an estimate, never reaching 1 before the job finishes, and None when no estimate
    holds. Absent while the job is queued and once it has settled.

    `thumbnail_url` is a small JPEG of the job's picture — the result, or the image the model
    worked from when it produced no file — for showing a page of jobs without downloading a
    page of full-size pictures. `input_url` is the picture the model worked
    from, so a result can be shown against it — and where you named a region, it is that
    region, so what was used is something you can look at rather than something to take on
    trust. It is not the file you uploaded but a new one written from it, not copied — so
    do not expect a hash of it to match your file's, and a JPEG or WebP is compressed
    again. Yours is removed on its own schedule. Both expire with the job's other pictures.

    `community` says the job was submitted by an account that was not paying — no credit,
    and no subscription with work left this period — and is on the community queue, which
    runs on shared, free capacity: it waits longer at busy times, and never stops. An empty
    balance is never itself a reason to refuse a job — paying moves work onto paid compute
    that scales with demand, not the right to submit — but on some models the community
    queue takes smaller files.
    `models()` lists each model's limits for your key, and a file over them is refused with
    the limit named.
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
    # `output_url` and the accessors beside it are **properties** below rather than fields: the
    # wire states each of those facts once, inside `result.output`, and reading them out of
    # it is a client library's job — `job.output_url` is nicer than reaching into a dict, and
    # it is how every example in this file is written.
    result: dict | None = None
    # A small JPEG of the job's picture, for a listing: the result, or the image the model
    # worked from when it produced no file. None until the job succeeds, where none could
    # be drawn, and once the job's pictures are deleted.
    thumbnail_url: str | None = None
    # Whole credits. A credit is not divisible, and a partial video second rounds up at the
    # charge rather than arriving as a fraction.
    credits: int | None = None
    eta_seconds: float | None = None
    progress: dict | None = None
    community: bool = False
    # Whether an automated check thought this job's images may fall outside what the service
    # allows: the part of your image the job worked on (all of it, unless you named a
    # region), and for some jobs the one that came back. `None` means they were not all
    # checked.
    flagged: bool | None = None
    # Whether this result is retained past the ordinary expiry, because its owner asked.
    # A kept result also keeps working links.
    kept: bool = False
    # Where this job stands with the public gallery, when you have offered it:
    # `{"status": "pending" | "approved", "post_id": ...}` while it is being reviewed or
    # shown, None when it is not offered — including after you withdraw it.
    gallery: dict | None = None
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
    def input_url(self) -> str | None:
        """The picture the model worked from, which is the region where you named one.

        Not the file you uploaded but a new one written from it, not copied — so do not
        expect a hash of it to match your file's, and a JPEG or WebP is compressed again. Yours
        goes on its own schedule. This is what went into the model, so a before-and-after is a
        true pair. None for a job that has not finished. A model that answers about a picture
        rather than producing one has it too: it is the picture that answer is about.
        """
        given = (self.result or {}).get("input")
        return given.get("url") if isinstance(given, dict) else None

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
            raise CraterViewError(self._no_file(), job_id=self.id)
        resp = _sent(requests.get, self.output_url, timeout=300)
        # A link is as old as the read that brought it, and storage answers one past its
        # expiry with 403 — so that is what a 403 almost always is, and a fresh read the fix.
        if resp.status_code == 403:
            raise CraterViewError(
                f"storage refused the result of job {self.id} (HTTP 403): its link has most "
                f'likely expired. Read the job again, job("{self.id}"), for fresh links.',
                403, job_id=self.id)
        if resp.status_code >= 400:
            raise CraterViewError(f"storage refused the result of job {self.id}: "
                                  f"HTTP {resp.status_code}", resp.status_code, job_id=self.id)
        return resp.content

    def _no_file(self) -> str:
        """Why there is no file to download, which depends on where the job stands."""
        if not self.done:
            return (f"job {self.id} has no result yet: it is still {self.status}. "
                    f"Wait for it with wait_for() first.")
        if self.status == "failed":
            code = f" ({self.error_code})" if self.error_code else ""
            why = self.error or "no reason was given"
            return f"job {self.id} failed, so it has no result: {why}{code}"
        # Succeeded, with no file. Either its model answers in `result` alone, or the file
        # has gone; a job read after its files expire looks the same as one that never had
        # any, so both are said rather than one guessed at.
        return (f"job {self.id} has no file to download: its model answers in `result` "
                f"alone, or its file has expired or been deleted")

    def save(self, path: str | Path) -> Path:
        """Write the result to `path`, and return the path written.

        A path with no suffix is given the one for the format that came back, which may not be
        the one asked for: `output_format` is a preference, and a picture that format cannot
        hold comes back as PNG (`output_content_type` says which). So `save("photo-4x")`
        writes `photo-4x.jpg` or `photo-4x.png`, whichever the result is. A path with a suffix
        is written as given.
        """
        path = Path(path)
        if not path.suffix:
            path = path.with_name(path.name + _RESULT_SUFFIXES.get(self.output_content_type, ""))
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
        # default is not None: `community` is False, and an older server that does not
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
        resp = _sent(self._session.request, method, f"{self.base_url}{path}",
                     timeout=self.timeout, **kwargs)
        trace_id = resp.headers.get("X-Trace-Id")
        if resp.status_code == 429:
            raise RateLimited(self._detail(resp),
                              retry_after=float(resp.headers.get("Retry-After", 0) or 0),
                              trace_id=trace_id, code=_code(resp))
        if resp.status_code >= 400:
            raise CraterViewError(self._detail(resp), resp.status_code, trace_id,
                                  code=_code(resp))
        # A 204 carries no body, so asking for JSON raises on a call that succeeded. Checked
        # by status rather than by looking for an empty body: this is the only response the
        # API sends without one, and a status is a fact rather than an inference.
        if resp.status_code == 204:
            return None
        try:
            return resp.json()
        except ValueError:
            raise CraterViewError(self._not_the_api(resp), resp.status_code, trace_id) from None

    def _detail(self, resp: requests.Response) -> str:
        """The API's own words for a refusal, or a sentence saying the answer was not its."""
        try:
            body = resp.json()
        except ValueError:
            return self._not_the_api(resp)
        return (_spoken(body.get("detail")) if isinstance(body, dict) else None) \
            or self._not_the_api(resp)

    def _not_the_api(self, resp: requests.Response) -> str:
        """For an answer that is not the API's JSON: an error page from something between here
        and the API, or a `base_url` that names something else. The body itself is left out —
        a page of markup buries the one line worth reading."""
        host = urlsplit(self.base_url).netloc or self.base_url
        said = f"{host} answered HTTP {resp.status_code} with something other than the API's JSON"
        if resp.status_code >= 500:
            return (said + ": a failure on the way to the API rather than a refusal. Worth "
                    "repeating — with the same idempotency key, for a submission, so it "
                    "cannot start a second job.")
        if resp.status_code == 429:
            return said + "."
        return said + f"; check that base_url is the API's address ({DEFAULT_BASE_URL} by default)."

    # ---------------------------------------------------------------------- public

    def models(self) -> list[dict]:
        """Available models: parameter schemas, prices, limits, and which queue you are on.

        Prices are published here, so the cost of a job is knowable before submitting it.

        `community` and the limits are answered *for the queue your key would use*. The API
        looks up the account and reports the queue a job from this key would land in — an
        account that is paying, by holding credit or by subscribing, runs on paid compute,
        which scales with demand, and one that is not gets the community queue,
        on shared, free capacity. So two keys asking at the same moment
        can get different answers, and paying changes yours.

        `community` here means what `Job.community` means on a submitted job. How long a
        wait will be is answered on the job, once you have one — `Job.eta_seconds`.

        A model that is available is not always listed — a model in trial, or being
        retired, stays usable by name while absent from this catalog.

        And a model that is listed is not always usable: `coming_soon` marks one that is
        announced but not yet in service, and submitting to it raises with 409 until it
        launches. Everything else published about it is final, so an integration can be
        written against it in advance. `video_coming_soon` says the same of one model's
        video, which is why `accepts` names no clip for it yet.

        `tenure` says what kind of model it is: `fixed` is a lasting part of the service,
        and `comet` is a utility or fun experiment. `test_model` marks a model for testing
        an integration rather than for use — `echo`, free, returning a result of the right
        size without improving the picture. CraterView's apps do not offer it; an app built
        on this catalog should leave it out too.

        In the limits, a limit is a positive number, or null where nothing bounds that axis
        for your key — for a setting's own limits (`max_input_megapixels_by_binning`), also
        where the model has no such setting. A limit is never 0.
        """
        return self._request("GET", "/v1/models")

    def upload(self, image: str | Path | bytes | BinaryIO,
               content_type: str | None = None) -> Upload:
        """Put an image in storage and return its key.

        Bytes go straight to object storage on a presigned URL, never through the API.

        The key comes back as an `Upload`: a `str` you pass to `submit()` as before, that
        also carries `megapixels` — the image's size, read from its header without decoding
        it — so `submit()` can tell the API what you sent and `eta_seconds` is estimated
        for your image rather than a typical one. `None` for a file the reader does not
        know, which changes nothing but the estimate.
        """
        # The type is what the bytes say they are, then what the file's name says, then
        # nothing: an undeclared upload is accepted and the file is identified when the job
        # runs. A type the API does not take is refused by this first request, before any
        # bytes are sent, with the list of what it does take.
        if isinstance(image, (str, Path)):
            path = Path(image)
            data = path.read_bytes()
            content_type = content_type or _sniff(data) or _guess_type(path)
        else:
            data = image if isinstance(image, bytes) else image.read()
            content_type = content_type or _sniff(data) or "application/octet-stream"
        # Sent, it would come back as a refusal of `content_length`, a field the caller never
        # wrote. Said here in their terms instead, before anything is requested.
        if not data:
            what = str(image) if isinstance(image, (str, Path)) else "the image given"
            raise CraterViewError(f"{what} is empty (0 bytes): there is nothing to upload")

        # The length is declared and then signed into the URL, so it must be the length
        # actually sent — storage refuses any other size with a signature error. `data` is
        # bytes by this point either way, so it is measured rather than guessed.
        slot = self._request("POST", "/v1/uploads",
                             json={"content_type": content_type, "content_length": len(data)})
        put = _sent(requests.put, slot["upload_url"], data=data,
                    headers={"Content-Type": content_type}, timeout=300)
        if put.status_code >= 400:
            raise CraterViewError(f"storage refused the upload: HTTP {put.status_code}",
                                  put.status_code)
        return Upload(slot["input_key"], megapixels=_megapixels(data))

    def submit(self, model: str, input_key: str, *, wait: float = 0,
               idempotency_key: str | None = None, webhook_url: str | None = None,
               custom_id: str | None = None, input_megapixels: float | None = None,
               **params) -> Job:
        """Queue a job. `wait` holds the response open for a settled result, for up to that
        many seconds; the API holds one for at most 30, so a longer wait is asked as 30.

        `input_megapixels` tells the API how big the input is (width × height ÷ 1,000,000),
        so `eta_seconds` is estimated for your image rather than a typical one. It changes
        nothing else — not the price, the queue or whether the job is accepted, which all
        read the file itself. Left unset, the size `upload()` read from the file is sent when
        `input_key` is the `Upload` it returned; pass a figure to override or supply one.

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
        # Held to the API's ceiling, so a longer wait is a long poll rather than a refusal.
        # None at all is the API's own default, and is left unsaid.
        wait = min(wait, MAX_SERVER_WAIT)
        path = f"/v1/jobs?wait={wait:g}" if wait > 0 else "/v1/jobs"
        body = {"model": model, "input_key": input_key, "params": params}
        if webhook_url:
            body["webhook_url"] = webhook_url
        if custom_id:
            body["custom_id"] = custom_id
        if input_megapixels is None and isinstance(input_key, Upload):
            input_megapixels = input_key.megapixels
        if input_megapixels is not None and input_megapixels > 0:
            body["input_megapixels"] = float(input_megapixels)
        payload = self._request("POST", path, json=body, headers=headers)
        return Job._from(payload, self)

    def job(self, job_id: str) -> Job:
        """One job, by the `id` the API gave it. Anything that is not a job id raises
        `InvalidId` without a request being made."""
        return Job._from(self._request("GET", f"/v1/jobs/{_issued('job', job_id, 'job_id')}"),
                         self)

    def jobs(self, limit: int = 50) -> Iterator[Job]:
        """Iterate the account's job history, newest first, paging transparently.

        Scoped to the account rather than to this key, so a key sees every job the account
        has run and not only the ones it submitted itself.

        `limit` is how many jobs one request fetches, not how many you get: iteration
        continues until the history runs out. 200 is the largest page the API serves, and a
        bigger number is fetched as 200 rather than sent and refused.
        """
        before = None
        while True:
            query = f"/v1/jobs?limit={min(limit, MAX_PAGE)}"
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
        A job is charged when it succeeds, so queued and running work is not in the
        figures yet, and a failed job costs nothing.

        The balance is `credits_remaining`; `credits_spent` is what the account has spent,
        all time, and `credits_granted` what it has been given. `credits_remaining` is
        always a number, floored at zero. A balance of zero never
        stops a submission: `community` says whether your next job runs on the community
        queue, which is free and can wait longer at busy times, or on paid compute.
        `jobs_in_flight` and `max_jobs_in_flight` are the state of the queue and are what a
        429 on submit is about.

        Account-wide, so every key on an account reports the same figures.
        """
        return self._request("GET", "/v1/usage")

    def webhook_secret(self) -> str:
        """The secret your callbacks are signed with, created the first time you ask or with
        your first delivery, whichever comes first.

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
        """Every key on the account, including revoked ones, by prefix rather than value,
        newest first.

        A revoked key stays listed so that a client which started failing can be explained.
        The API serves the list a page at a time; this fetches every page and returns them
        together.
        """
        found: list[dict] = []
        before = None
        while True:
            query = f"/v1/keys?limit={MAX_PAGE}" + (f"&before={before}" if before else "")
            page = self._request("GET", query)
            found.extend(page["data"])
            cursor = page.get("next_before")
            # As in `jobs`: a missing or non-advancing cursor stops rather than spinning.
            if not page.get("has_more") or not cursor or cursor == before:
                return found
            before = cursor

    def create_key(self, name: str = "api") -> dict:
        """Mint another key. **The value is in this response and nowhere else.**

        Keys are stored as hashes, so one that is not written down has to be replaced rather
        than recovered. Several keys on an account is the ordinary arrangement — one per
        service or environment — and they share the account's credits and history.

        This is also how you rotate without downtime: create the new key, move your clients
        onto it, then revoke the old one.

        An account is given a limited number of new keys an hour, whichever of its keys
        asks: past it this raises `RateLimited`, whose `retry_after` says when to ask again.
        """
        return self._request("POST", "/v1/keys", json={"name": name})

    def revoke_key(self, key_id: str) -> None:
        """Stop a key working. Immediate, and not reversible.

        You cannot revoke the key this client is authenticating with: it is refused with
        status 409, because it would leave you unable to make another. Create a replacement,
        move onto it, then revoke this one.

        `key_id` is a key's `id` from `keys()` or `create_key()`, never its value or its
        prefix. Anything else raises `InvalidId` without a request being made.
        """
        self._request("DELETE", f"/v1/keys/{_issued('key', key_id, 'key_id')}")

    def wait_for(self, job: Job | str, timeout: float = 600, poll: float = 1.0) -> Job:
        """Block until a job settles. Prefer `wait=` on submit for short jobs.

        Past `timeout` this raises `CraterViewError` with the job's `job_id`. Only the wait has
        ended: the job carries on, and is collected by calling this again with that id.

        A read that gets no answer, a failure on the way to the API or a request to slow down
        is not the job's, and the wait goes on through it until `timeout`. Anything else it
        raises carries the `job_id` too, so no failure here loses the job.
        """
        job_id = _issued("job", job.id if isinstance(job, Job) else job, "job")
        current = job if isinstance(job, Job) else None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                current = self.job(job_id)
            except CraterViewError as failure:
                if not (_worth_asking_again(failure) or isinstance(failure, RateLimited)):
                    failure.job_id = failure.job_id or job_id
                    raise
                time.sleep(max(poll, getattr(failure, "retry_after", None) or 0))
                continue
            if current.done:
                return current
            time.sleep(poll)
        where = f"still {current.status}" if current else "not finished"
        raise CraterViewError(
            f"job {job_id} is {where} after {timeout:g}s of waiting. It has not failed: it "
            f"carries on, and is charged only if it succeeds. Collect it with "
            f'wait_for("{job_id}") or job("{job_id}"); submitting the request again starts a '
            f"second job, charged as well.", job_id=job_id)

    def run(self, image: str | Path | bytes | BinaryIO, *, model: str = "cv-enhance-v3",
                wait: float = MAX_SERVER_WAIT, timeout: float = 600,
                raise_on_failure: bool = True, **params) -> Job:
        """Upload, submit and wait, in one call — the common case.

        Uses server-side waiting first so a fast job costs a single round trip, then falls
        back to polling for anything slower. The server is asked to hold the submission for at
        most half this client's own `timeout`, so its answer arrives before the client stops
        listening for it.

        Past `timeout` the job is not given up on, only waited for: the error carries its
        `job_id`, to collect it by with `wait_for`.

        **One submission, asked at most twice.** A submission that got no answer, or met a
        failure on the platform's side, may have made the job before it failed, so it is asked
        once more under one idempotency key, and the API answers with the job the first attempt
        made if it made one — never a second. Pass `idempotency_key` to choose the key. Refused
        both times by the API's own 503, which it answers before anything is recorded, the
        error says so in its words.
        """
        input_key = self.upload(image)
        key = params.pop("idempotency_key", None) or new_idempotency_key()

        def submitted() -> Job:
            return self.submit(model, input_key, wait=min(wait, self.timeout / 2),
                               idempotency_key=key, **params)

        first: CraterViewError | None = None
        try:
            try:
                job = submitted()
            except CraterViewError as failure:
                if not _worth_asking_again(failure):
                    raise
                first = failure
                time.sleep(_RESUBMIT_AFTER)
                job = submitted()
        except CraterViewError as failure:
            if not _worth_asking_again(failure):
                raise
            if _queued_nothing(first) and _queued_nothing(failure):
                raise type(failure)(
                    f"the submission was asked twice, and both times CraterView said why: "
                    f"{failure}", failure.status, failure.trace_id) from failure
            raise type(failure)(
                f"the submission was asked twice and answered neither time ({failure}), so a "
                f"job may have been made; if it was, it runs and is charged only if it "
                f"succeeds. Look for it with jobs(), newest first, before running this again: "
                f"run() uploads afresh, so repeating it starts a second job.",
                failure.status, failure.trace_id) from failure
        if not job.done:
            job = self.wait_for(job, timeout=timeout)
        if raise_on_failure and not job.succeeded:
            raise JobFailed(job.error or "job failed", job.error_code, job.id)
        return job


def _spoken(detail: Any) -> str | None:
    """The API's `detail` as one sentence, or None where it holds no words.

    Usually a string. A request its validator refuses comes back as a list instead, one entry
    per field, which reads here as "field: what is wrong" — and without the validator's own
    "Value error, " in front of a sentence the API wrote for a reader.
    """
    if isinstance(detail, str):
        return detail or None
    if not isinstance(detail, list):
        return None
    said = []
    for entry in detail:
        if not isinstance(entry, dict) or not entry.get("msg"):
            continue
        message = str(entry["msg"]).removeprefix("Value error, ")
        # A location of one part — the body as a whole — names no field worth printing.
        where = entry.get("loc") or []
        said.append(f"{where[-1]}: {message}" if len(where) > 1 else message)
    return "; ".join(said) or None


class Upload(str):
    """The key of an uploaded file — a plain `str` for `submit()` — with what the client
    learned about the file before sending it: `megapixels`, or `None` when it could not
    tell. Comparing, hashing and printing it behave as the key alone."""

    megapixels: float | None

    def __new__(cls, key: str, megapixels: float | None = None) -> "Upload":
        made = super().__new__(cls, key)
        made.megapixels = megapixels
        return made


def _megapixels(data: bytes) -> float | None:
    """Width × height ÷ 10⁶ from the file's header, or None for a file the readers do not
    know. Opening is lazy — nothing is decoded — and nothing here may fail an upload: a
    size the client cannot read is simply not declared."""
    try:
        from io import BytesIO

        from PIL import Image

        try:
            from pi_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass
        with Image.open(BytesIO(data)) as im:
            width, height = im.size
        return round(width * height / 1e6, 4) or None
    except Exception:  # noqa: BLE001 - a size the client cannot read is not declared
        return None


def _sniff(data: bytes) -> str | None:
    """The media type the bytes are, from the signature at their start, or None.

    Formats the API does not take are named too, so that the refusal says what the file is
    rather than that it is unknown.
    """
    head = data[:64]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "image/tiff"
    if head.startswith((b"\xff\x0a", b"\x00\x00\x00\x0cJXL \r\n\x87\n")):
        return "image/jxl"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[4:8] == b"ftyp":
        # An ISO base media file names its brands: the major one, then the compatible ones
        # until the end of the `ftyp` box. An AVIF may declare the generic `mif1` first and
        # `avif` among the rest, so every brand is read before deciding.
        end = min(int.from_bytes(head[:4], "big"), len(head))
        brands = {head[8:12]} | {head[i:i + 4] for i in range(16, end - 3, 4)}
        if brands & {b"avif", b"avis"}:
            return "image/avif"
        if head[8:12] in (b"heic", b"heix", b"heim", b"heis"):
            return "image/heic"
        if head[8:12] in (b"mif1", b"mif2", b"msf1"):
            return "image/heif"
    return None


# The suffix each format a result comes back in is saved under.
_RESULT_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                    "video/mp4": ".mp4"}

# The media type a file's name implies, for bytes `_sniff` cannot name.
_EXTENSIONS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif",
    ".jxl": "image/jxl", ".gif": "image/gif", ".bmp": "image/bmp",
}


def _guess_type(path: Path) -> str:
    return _EXTENSIONS.get(path.suffix.lower(), "application/octet-stream")
