"""Fail when a detect-secrets JSON report contains any finding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def verify_report(*, report: Path) -> None:
    document: Any = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("detect-secrets report must be a JSON object")
    results = document.get("results")
    if not isinstance(results, dict):
        raise ValueError("detect-secrets report has no results object")
    for path, items in results.items():
        # A scanned file is reported as a list of findings, empty or not. Anything
        # else means the report is not the shape this gate can read, and a secret
        # gate that cannot read its input has to fail closed.
        if not isinstance(items, list):
            raise ValueError(f"detect-secrets report entry for {path!r} is not a list of findings")
    flagged = {path: items for path, items in results.items() if items}
    if flagged:
        findings = sum(len(items) for items in flagged.values())
        raise ValueError(f"detect-secrets reported {findings} finding(s) in {len(flagged)} file(s)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        required=True,
        type=Path,
        help="JSON report produced by detect-secrets scan",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    verify_report(report=args.report.resolve(strict=True))
    print("Verified detect-secrets report: no findings")


if __name__ == "__main__":
    main()
