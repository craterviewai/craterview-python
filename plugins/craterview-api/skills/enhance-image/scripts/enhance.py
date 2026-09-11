#!/usr/bin/env python3
"""Enhance or restore one image with CraterView, from the command line.

    python enhance.py photo.jpg --scale 4 -o photo-4x.png
    python enhance.py scan.jpg --model cv-restore-v1 --param mode=full --param monochrome=true
    python enhance.py photo.jpg --model cv-content-check-v1        # prints the result, no file

Needs `pip install craterview` and CRATERVIEW_API_KEY in the environment. The key is read
from there and nowhere else, on purpose: a key on a command line ends up in shell history.

Exit status is 0 on success, 1 for anything the API refused or the job failed on, and the
message says which. A failed job is not charged.
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
    sys.exit("the craterview package is not installed: pip install craterview")


def _value(text: str):
    """`--param k=v` values are typed: true/false, integers, then strings."""
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path, help="the file to send")
    parser.add_argument("--model", default="cv-enhance-v3",
                        help="which model to run; `cv.models()` lists them (default: %(default)s)")
    parser.add_argument("--scale", type=int,
                        help="enlargement factor 1-4; only the enhancement model takes one")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                        help="any other model parameter, repeatable")
    parser.add_argument("-o", "--output", type=Path,
                        help="where to save the result (default: <name>-<model>.<ext>)")
    parser.add_argument("--timeout", type=float, default=600,
                        help="seconds to wait for the job in total (default: %(default)s)")
    args = parser.parse_args(argv)

    key = os.environ.get("CRATERVIEW_API_KEY")
    if not key:
        return _fail("CRATERVIEW_API_KEY is not set. Sign in at https://craterview.ai, open the "
                     "dashboard's Developer API panel, Show my API key, and export it.")
    if not args.image.is_file():
        return _fail(f"{args.image} is not a file")

    params: dict = {}
    for item in args.param:
        if "=" not in item:
            return _fail(f"--param wants KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        params[k] = _value(v)
    if args.scale is not None:
        params["scale"] = args.scale

    cv = CraterView(api_key=key)
    try:
        job = cv.run(args.image, model=args.model, timeout=args.timeout,
                     raise_on_failure=False, **params)
    except CraterViewError as e:
        return _fail(f"the API refused the request: {e}")

    if not job.succeeded:
        return _fail(f"job {job.id} failed: {job.error} ({job.error_code})")

    if job.output_url is None:
        # A model with no file to hand back answers in `result`.
        print(json.dumps(job.result, indent=2))
        return 0

    out = args.output or _default_output(args.image, args.model, job.output_content_type)
    job.save(out)
    print(f"{out}  ({job.credits} credit{'s' if job.credits != 1 else ''}, job {job.id})")
    return 0


def _default_output(source: Path, model: str, content_type: str | None) -> Path:
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        content_type or "", source.suffix or ".png")
    return source.with_name(f"{source.stem}-{model}{ext}")


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
