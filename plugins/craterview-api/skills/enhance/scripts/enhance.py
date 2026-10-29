#!/usr/bin/env python3
"""Enhance or restore one image with CraterView, from the command line.

    python enhance.py photo.jpg --model echo --scale 2            # free: proves the key and the plumbing
    python enhance.py photo.jpg --scale 4 -o photo-4x       # the result's own suffix is added
    python enhance.py scan.jpg --model cv-restore-v1 --param monochrome=true
    python enhance.py scan.jpg --model cv-restore-v1 --param "context=A soldier in a red tunic"
    python enhance.py photo.jpg --model cv-content-check-v1        # prints the result, no file

Needs `pip install "craterview>=0.4.18"` and CV_API_KEY in the environment. The key is read
from there and nowhere else, on purpose: a key on a command line ends up in shell history.

Exit status is 0 on success and 1 for anything that went wrong — the network, a refusal
from the API or from storage, a job that failed, or one still going when --timeout ran
out — and the message says which. A failed job is not charged; one still going carries on,
and is charged only if it succeeds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from craterview import CraterView, CraterViewError
except ImportError:
    sys.exit('the craterview package is not installed: pip install "craterview>=0.4.18"')


def _value(text: str, kind: str | None):
    """A `--param k=v` value as the type the model's schema gives the parameter.

    Free text stays text, so `context=1975` is a year and not a number. A value that does
    not read as its type is sent as written, and the API's refusal names the parameter. A
    parameter the catalog does not describe is typed by its look: true/false, integers,
    then strings.
    """
    lowered = text.lower()
    if kind == "string":
        return text
    if kind in ("boolean", None) and lowered in ("true", "false"):
        return lowered == "true"
    cast = {"integer": int, "number": float, None: int}.get(kind)
    if cast is not None:
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path, help="the file to send")
    parser.add_argument("--model", default="cv-enhance-v3",
                        help="which model to run; `cv.models()` lists them (default: %(default)s)")
    parser.add_argument("--scale", type=float,
                        help="enlargement factor from 1 to 4, not necessarily whole; only a "
                             "model that enlarges takes one, and --param width=... or "
                             "height=... sizes the result in pixels instead")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="any other model parameter, repeatable")
    parser.add_argument("-o", "--output", type=Path,
                        help="where to save the result (default: <name>-<model>.<ext>)")
    parser.add_argument("--timeout", type=float, default=600,
                        help="seconds to wait for the job in total (default: %(default)s)")
    args = parser.parse_args(argv)

    key = os.environ.get("CV_API_KEY")
    if not key:
        return _fail("CV_API_KEY is not set. Sign in at https://craterview.ai, open the "
                     "dashboard's Developer API panel, Show my API key, and export it.")
    if not args.image.is_file():
        return _fail(f"{args.image} is not a file")

    cv = CraterView(api_key=key)
    params: dict = {}
    properties: dict = {}
    if args.param:
        try:
            listed = {m["id"]: m for m in cv.models()}
        except CraterViewError as e:
            return _fail(_said(e))
        properties = (listed.get(args.model) or {}).get("params_schema", {}).get("properties", {})
    for item in args.param:
        if "=" not in item:
            return _fail(f"--param wants KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        params[k] = _value(v, properties.get(k, {}).get("type"))
    if args.scale is not None:
        params["scale"] = args.scale

    try:
        job = cv.run(args.image, model=args.model, timeout=args.timeout,
                     raise_on_failure=False, **params)
    except CraterViewError as e:
        return _fail(_said(e))

    if not job.succeeded:
        code = f" ({job.error_code})" if job.error_code else ""
        return _fail(f"job {job.id} failed: {job.error or 'no reason was given'}{code}")

    if job.output_url is None:
        # A model with no file to hand back answers in `result`.
        print(json.dumps(job.result, indent=2))
        return 0

    try:
        out = job.save(args.output or _default_output(args.image, args.model))
    except CraterViewError as e:
        return _fail(_said(e))
    print(f"{out}  ({job.credits} credit{'s' if job.credits != 1 else ''}, job {job.id})")
    return 0


def _default_output(source: Path, model: str) -> Path:
    """Beside the source, with no suffix: `save` gives it the one for the format that came
    back, which may not be the one sent."""
    return source.with_name(f"{source.stem}-{model}")


def _said(error: CraterViewError) -> str:
    """What went wrong, named as what it was, and the job it was about where there is one.

    The client's two network errors are also the builtins, which is what is checked here. Its
    message says whether a job may have been made, so nothing here adds "try again". Only an
    answer of 4xx is the API's refusal; a 5xx is a failure on its side, with the trace id to
    quote. Storage's refusals and a wait that ran out say what they are in their own words.
    """
    job = f" (job {error.job_id})" if error.job_id else ""
    if isinstance(error, ConnectionError):
        return f"could not reach CraterView: {error}{job}"
    if isinstance(error, TimeoutError):
        return f"CraterView did not answer in time: {error}{job}"
    if error.trace_id and error.status and error.status < 500:
        return f"the API refused the request: {error} (trace id {error.trace_id}){job}"
    if error.trace_id:
        return f"CraterView failed on its side: {error} (trace id {error.trace_id}){job}"
    return f"{error}{job}"


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
