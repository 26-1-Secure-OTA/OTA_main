import argparse
import json
import math
import random
import re
from collections import Counter
from pathlib import Path

from .feature_schema import FEATURE_NAMES, validate_feature_definition, validate_feature_values


SCENARIO_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]{3,}$")
POLICY_DECISIONS = {"ALLOW", "HOLD", "BLOCK"}
REQUIRED_FIELDS = {
    "scenario_id", "secondary_id", "policy_decision", "update_attempted",
    "success", "telemetry_valid",
}


class DatasetValidationError(ValueError):
    pass


def _reject_constant(value):
    raise DatasetValidationError(f"non-finite JSON number is forbidden: {value}")


def _validate_optional_number(row, name, *, ratio=False):
    value = row.get(name)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DatasetValidationError(f"{name} must be a finite number or null")
    if ratio and not 0.0 <= value <= 1.0:
        raise DatasetValidationError(f"{name} must be between 0 and 1")


def validate_log_row(row: dict) -> None:
    if not isinstance(row, dict):
        raise DatasetValidationError("row must be a JSON object")
    missing = sorted(REQUIRED_FIELDS - row.keys())
    if missing:
        raise DatasetValidationError(f"required fields missing: {missing}")
    scenario_id = row["scenario_id"]
    if not isinstance(scenario_id, str) or (
        scenario_id != "UNSPECIFIED" and not SCENARIO_RE.fullmatch(scenario_id)
    ):
        raise DatasetValidationError(f"invalid scenario_id: {scenario_id!r}")
    if not isinstance(row["secondary_id"], str) or not row["secondary_id"]:
        raise DatasetValidationError("secondary_id must be non-empty string")
    if row["policy_decision"] not in POLICY_DECISIONS:
        raise DatasetValidationError("policy_decision must be ALLOW, HOLD, or BLOCK")
    if not isinstance(row["update_attempted"], bool):
        raise DatasetValidationError("update_attempted must be boolean")
    if row["success"] is not None and not isinstance(row["success"], bool):
        raise DatasetValidationError("success must be boolean or null")
    if row["telemetry_valid"] is not None and not isinstance(row["telemetry_valid"], bool):
        raise DatasetValidationError("telemetry_valid must be boolean or null")
    for name in ("app_flash_free_ratio", "free_flash_ratio", "recent_retry_rate"):
        _validate_optional_number(row, name, ratio=True)
    for name in FEATURE_NAMES:
        _validate_optional_number(row, name, ratio=(name == "app_flash_free_ratio"))


def read_jsonl(path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    with Path(path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_constant=_reject_constant)
                validate_log_row(row)
                rows.append(row)
            except (json.JSONDecodeError, UnicodeDecodeError, DatasetValidationError) as exc:
                errors.append(f"line {line_number}: {exc}")
    return rows, errors


def build_training_rows(rows: list[dict], feature_names=FEATURE_NAMES) -> tuple[list[dict], list[str]]:
    validate_feature_definition(feature_names)
    output, rejected = [], []
    for index, row in enumerate(rows):
        try:
            validate_log_row(row)
        except DatasetValidationError as exc:
            rejected.append(f"row {index}: {exc}")
            continue
        if not (
            row["policy_decision"] == "ALLOW"
            and row["update_attempted"] is True
            and row["success"] in (True, False)
            and row["scenario_id"] != "UNSPECIFIED"
            and row["telemetry_valid"] is True
        ):
            continue
        try:
            values = validate_feature_values(row)
        except ValueError as exc:
            rejected.append(f"row {index}: {exc}")
            continue
        output.append({
            "scenario_id": row["scenario_id"],
            "secondary_id": row["secondary_id"],
            "features": values,
            "failure_label": 0 if row["success"] is True else 1,
        })
    return output, rejected


def group_split(rows: list[dict], seed: int = 42) -> dict[str, list[dict]]:
    groups = sorted({row["scenario_id"] for row in rows})
    if len(groups) < 3:
        raise DatasetValidationError(
            f"at least 3 scenario groups are required for train/validation/test; got {len(groups)}"
        )
    random.Random(seed).shuffle(groups)
    count = len(groups)
    validation_count = max(1, round(count * 0.2))
    test_count = max(1, round(count * 0.2))
    if validation_count + test_count >= count:
        validation_count = test_count = 1
    train_count = count - validation_count - test_count
    group_sets = {
        "train": set(groups[:train_count]),
        "validation": set(groups[train_count:train_count + validation_count]),
        "test": set(groups[train_count + validation_count:]),
    }
    if any(group_sets[a] & group_sets[b] for a, b in (("train", "validation"), ("train", "test"), ("validation", "test"))):
        raise AssertionError("scenario group leakage detected")
    return {
        name: [row for row in rows if row["scenario_id"] in selected]
        for name, selected in group_sets.items()
    }


def split_summary(splits: dict[str, list[dict]]) -> dict:
    return {
        name: {
            "rows": len(rows),
            "groups": len({row["scenario_id"] for row in rows}),
            "labels": dict(Counter(row["failure_label"] for row in rows)),
        }
        for name, rows in splits.items()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log_path")
    args = parser.parse_args()
    rows, errors = read_jsonl(args.log_path)
    training, rejected = build_training_rows(rows)
    print(json.dumps({"valid_rows": len(rows), "validation_errors": errors, "training_rows": len(training), "rejected": rejected}, indent=2))
    if errors:
        raise SystemExit(1)
    print(json.dumps(split_summary(group_split(training)), indent=2))


if __name__ == "__main__":
    main()
