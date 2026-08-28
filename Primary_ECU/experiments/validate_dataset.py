from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional


class DatasetValidationError(ValueError):
    pass


def _scenario(row: dict) -> Optional[str]:
    return (
        row.get("scenario_id")
        or row.get("scenario")
        or (row.get("pre_status") or {}).get("scenario")
    )


def _policy_decision(row: dict) -> Optional[str]:
    return (
        row.get("preflight_decision")
        or row.get("policy_decision")
        or row.get("safety_decision")
    )


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetValidationError(
                    f"invalid JSON at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise DatasetValidationError(
                    f"line {line_number} is not a JSON object"
                )
            rows.append(row)
    return rows


def validate_rows(
    rows: Iterable[dict],
    *,
    minimum_attempts: int = 60,
) -> dict:
    attempted = [row for row in rows if row.get("update_attempted") is True]
    allow_attempts = [
        row for row in attempted if _policy_decision(row) == "ALLOW"
    ]
    errors = []

    if len(allow_attempts) < minimum_attempts:
        errors.append(
            f"expected at least {minimum_attempts} ALLOW attempts, "
            f"found {len(allow_attempts)}"
        )

    attempt_ids = [row.get("attempt_id") for row in allow_attempts]
    if any(not value for value in attempt_ids):
        errors.append("every ALLOW attempt must have attempt_id")
    present_ids = [value for value in attempt_ids if value]
    if len(present_ids) != len(set(present_ids)):
        errors.append("attempt_id values must be unique")

    if any(not row.get("campaign_id") for row in allow_attempts):
        errors.append("every ALLOW attempt must have campaign_id")
    if any(not row.get("secondary_id") for row in allow_attempts):
        errors.append("every ALLOW attempt must have secondary_id")
    if any(not _scenario(row) for row in allow_attempts):
        errors.append("every ALLOW attempt must have a scenario")

    successes = sum(row.get("success") is True for row in allow_attempts)
    failures = sum(row.get("success") is False for row in allow_attempts)
    if successes == 0:
        errors.append("dataset has no successful ALLOW attempts")
    if failures == 0:
        errors.append("dataset has no failed ALLOW attempts")
    if successes + failures != len(allow_attempts):
        errors.append("every ALLOW attempt must have boolean success")

    if errors:
        raise DatasetValidationError("; ".join(errors))

    scenario_counts = Counter(_scenario(row) for row in allow_attempts)
    board_counts = Counter(row["secondary_id"] for row in allow_attempts)
    return {
        "valid": True,
        "allow_attempts": len(allow_attempts),
        "campaigns": len({row["campaign_id"] for row in allow_attempts}),
        "successes": successes,
        "failures": failures,
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "board_counts": dict(sorted(board_counts.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an OTA fault-injection JSONL dataset.",
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--minimum-attempts", type=int, default=60)
    args = parser.parse_args()
    summary = validate_rows(
        read_jsonl(args.dataset),
        minimum_attempts=args.minimum_attempts,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
