"""Live smoke test for POST /extract's Bedrock path (B2). Run by a human with their own AWS access.

  1. Configure access the normal way (for example `aws configure sso` then `$env:AWS_PROFILE="..."`).
     Never paste credentials into a chat, a file in this repo, or a command history you share.
  2. Set the two settings this project refuses to guess:
       $env:AWS_REGION       = "<region where your model access is enabled>"
       $env:BEDROCK_MODEL_ID = "<model id or inference profile id you have access to>"
  3. python scripts/live_extract_smoke.py

It sends the fictional descriptions from fixtures/extraction.json through the real BedrockExtractor
(one Converse call, plus at most one repair call) and prints a SANITIZED report: model id, region,
AWS request id(s), token counts, latency, and how the draft compares with the fixture's expected
draft. It prints no description text, prompt, model output or credentials.

A DIFF is information, not necessarily a bug: a real model may legitimately drop a value whose quote
it could not support. Paste the report into docs/compliance/B2.md under "Live verification".
Cost: one or two small model calls.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.bedrock import BedrockClient, load_bedrock_settings  # noqa: E402
from backend.config import ConfigError  # noqa: E402
from backend.errors import ApiError  # noqa: E402
from backend.extraction import BedrockExtractor  # noqa: E402
from contracts.models import ExtractRequest  # noqa: E402


class _Collect(logging.Handler):
    """Collects the client's own sanitized log lines (they hold ids, counts and timings only)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.calls: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            return
        if data.get("event") == "bedrock_call":
            self.calls.append(data)


def run_smoke(extractor: BedrockExtractor, collector: _Collect | None = None) -> dict[str, Any]:
    """Run the canonical request and return a sanitized report. Raises ApiError on a service failure."""
    fx = json.loads((ROOT / "fixtures" / "extraction.json").read_text(encoding="utf-8"))
    collector = collector or _Collect()
    logger = logging.getLogger("trademind.bedrock")
    logger.addHandler(collector)
    try:
        response = extractor(ExtractRequest.model_validate(fx["request"]))
    finally:
        logger.removeHandler(collector)

    got = response.model_dump(mode="json")["draft"]
    want = fx["response"]["draft"]
    diffs: list[str] = []
    for i, (g, w) in enumerate(zip(got["businesses"], want["businesses"], strict=False)):
        for key in ("capacityMinutes", "coordinationMinutes"):
            if g[key] != w[key]:
                diffs.append(f"businesses[{i}].{key}: got {g[key]!r}, fixture {w[key]!r}")
        if sorted(map(json.dumps, g["estimates"])) != sorted(map(json.dumps, w["estimates"])):
            diffs.append(f"businesses[{i}].estimates differ")
    if got["deliverables"] != want["deliverables"]:
        diffs.append("deliverables differ")
    if response.missingFields != fx["response"]["missingFields"]:
        diffs.append(f"missingFields: got {response.missingFields}, fixture {fx['response']['missingFields']}")

    return {
        "mode": response.mode.root,
        "bedrockCalls": collector.calls,
        "draftMatchesFixture": not diffs,
        "differences": diffs,
        "missingFields": response.missingFields,
        "provenanceCount": len(response.provenance),
        "confirmedPresent": "confirmed" in json.dumps(got),
    }


def main() -> int:
    try:
        settings = load_bedrock_settings(os.environ)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if settings is None:
        print("Set AWS_REGION and BEDROCK_MODEL_ID first (this project never guesses them). See the top of this file.", file=sys.stderr)
        return 2

    print(f"Calling Bedrock: model={settings.model_id} region={settings.region} structuredOutput={settings.structured_output}")
    extractor = BedrockExtractor(BedrockClient(settings))
    try:
        report = run_smoke(extractor)
    except ApiError as exc:
        print(json.dumps({"result": "FAILED", "httpStatus": exc.status, "code": exc.code, "message": exc.message}, indent=2))
        return 1
    print(json.dumps({"result": "COMPLETED", **report}, indent=2))
    return 0 if report["mode"] == "live" and not report["confirmedPresent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
