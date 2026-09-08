"""Controlled rolling retraining for the hybrid Isolation Forest model."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
from typing import Iterable, Mapping

from .feature_schema import SELECTED_RISK_FEATURES


DEFAULT_RETRAIN_BATCH_ROWS = 30
DEFAULT_ROWS_PER_BOARD = 100
MAX_ACCEPTED_PHYSICAL_RISK = 0.95
ELIGIBLE_SCENARIOS = frozenset({"NORMAL", "UNSPECIFIED"})


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ADAPTIVE_DATA_INVALID:{path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"ADAPTIVE_DATA_INVALID:{path}:{line_number}")
        rows.append(row)
    return rows


def is_adaptive_training_eligible(row: Mapping[str, object]) -> bool:
    """Accept only real, successful, non-fault OTA rows trusted by the model."""
    if not isinstance(row, Mapping):
        return False
    if row.get("data_source") != "BOARD":
        return False
    if str(row.get("scenario_id") or "").upper() not in ELIGIBLE_SCENARIOS:
        return False
    if str(row.get("board_scenario") or "").upper() != "NORMAL":
        return False
    if row.get("policy_preflight_decision") != "ALLOW":
        return False
    if row.get("update_attempted") is not True or row.get("success") is not True:
        return False
    if row.get("telemetry_valid") is not True or row.get("power_good") is not True:
        return False
    if row.get("health") != "OK" or row.get("failure_reason_code") is not None:
        return False
    if row.get("ai_mode_used") != "ISOLATION_FOREST":
        return False
    physical_risk = row.get("ai_physical_anomaly_risk")
    if not _is_number(physical_risk) or not 0.0 <= physical_risk <= MAX_ACCEPTED_PHYSICAL_RISK:
        return False

    features = row.get("features")
    if not isinstance(features, Mapping):
        return False
    for name in SELECTED_RISK_FEATURES:
        if not _is_number(features.get(name)):
            return False
    return True


def _training_row(row: Mapping[str, object]) -> dict:
    features = row.get("features") or row.get("ai_features")
    if not isinstance(features, Mapping):
        raise ValueError("ADAPTIVE_TRAINING_FEATURES_MISSING")
    return {
        "secondary_id": row.get("secondary_id"),
        "collected_at": row.get("collected_at"),
        "attempt_id": row.get("attempt_id"),
        "training_source": row.get("training_source", "SUCCESSFUL_BOARD_OTA"),
        "ai_features": {
            name: features.get(name)
            for name in SELECTED_RISK_FEATURES
        },
    }


def _rolling_rows(
    bootstrap_rows: Iterable[dict],
    eligible_rows: Iterable[dict],
    rows_per_board: int,
) -> list[dict]:
    per_board: dict[str, list[dict]] = {}
    for source, rows in (
        ("BOOTSTRAP_BASELINE", bootstrap_rows),
        ("SUCCESSFUL_BOARD_OTA", eligible_rows),
    ):
        for row in rows:
            training = _training_row(dict(row, training_source=source))
            secondary_id = training.get("secondary_id")
            if not isinstance(secondary_id, str) or not secondary_id:
                raise ValueError("ADAPTIVE_SECONDARY_ID_INVALID")
            per_board.setdefault(secondary_id, []).append(training)

    selected = []
    for secondary_id in sorted(per_board):
        selected.extend(per_board[secondary_id][-rows_per_board:])
    if not selected:
        raise ValueError("ADAPTIVE_DATASET_EMPTY")
    return selected


def _atomic_write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def maybe_retrain(
    *,
    experiment_log_path: str | Path,
    bootstrap_dataset_path: str | Path,
    model_path: str | Path,
    metadata_path: str | Path,
    retrain_batch_rows: int = DEFAULT_RETRAIN_BATCH_ROWS,
    rows_per_board: int = DEFAULT_ROWS_PER_BOARD,
) -> dict:
    """Retrain after a batch of new eligible rows; the new model starts next OTA."""
    if retrain_batch_rows <= 0 or rows_per_board <= 0:
        raise ValueError("ADAPTIVE_CONFIG_INVALID")

    experiment_log = Path(experiment_log_path)
    bootstrap_dataset = Path(bootstrap_dataset_path)
    model = Path(model_path)
    metadata = Path(metadata_path)
    lock_path = metadata.with_suffix(metadata.suffix + ".retrain.lock")

    with _exclusive_lock(lock_path):
        eligible_rows = [
            row for row in _read_jsonl(experiment_log)
            if is_adaptive_training_eligible(row)
        ]
        try:
            current_metadata = json.loads(metadata.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("ADAPTIVE_METADATA_INVALID") from exc

        adaptive = current_metadata.get("adaptive_training") or {}
        previously_seen = int(adaptive.get("eligible_rows_seen", 0))
        current_count = len(eligible_rows)
        new_count = max(0, current_count - previously_seen)
        if new_count < retrain_batch_rows:
            return {
                "status": "WAITING_FOR_BATCH",
                "eligible_rows": current_count,
                "new_eligible_rows": new_count,
                "required_new_rows": retrain_batch_rows,
            }

        bootstrap_rows = _read_jsonl(bootstrap_dataset)
        training_rows = _rolling_rows(
            bootstrap_rows,
            eligible_rows,
            rows_per_board,
        )
        dataset_path = metadata.with_name("adaptive-training-v2.jsonl")
        _atomic_write_jsonl(dataset_path, training_rows)

        # Local import keeps OTA fallback usable when AI dependencies are absent.
        from .model_loader import load_model
        from .train_isolation_forest import save_artifacts, train

        pipeline, candidate_metadata = train(dataset_path)
        candidate_metadata["adaptive_training"] = {
            "enabled": True,
            "eligible_rows_seen": current_count,
            "new_eligible_rows": new_count,
            "retrain_batch_rows": retrain_batch_rows,
            "rows_per_board": rows_per_board,
            "training_rows": len(training_rows),
            "bootstrap_dataset": str(bootstrap_dataset),
            "adaptive_dataset": str(dataset_path),
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "eligibility": {
                "data_source": "BOARD",
                "board_scenario": "NORMAL",
                "policy_preflight_decision": "ALLOW",
                "success": True,
                "telemetry_valid": True,
                "power_good": True,
                "health": "OK",
                "maximum_physical_anomaly_risk": MAX_ACCEPTED_PHYSICAL_RISK,
            },
        }
        candidate_model = model.with_name(model.name + ".candidate.tmp")
        candidate_metadata_path = metadata.with_name(
            metadata.name + ".candidate.tmp"
        )
        saved = save_artifacts(
            pipeline,
            candidate_metadata,
            candidate_model,
            candidate_metadata_path,
        )
        load_model(
            candidate_model,
            candidate_metadata_path,
            expected_type="ISOLATION_FOREST",
            expected_schema_version=3,
        )

        previous_model = model.with_name(model.stem + ".previous" + model.suffix)
        previous_metadata = metadata.with_name(
            metadata.stem + ".previous" + metadata.suffix
        )
        shutil.copy2(model, previous_model)
        shutil.copy2(metadata, previous_metadata)
        try:
            os.replace(candidate_model, model)
            os.replace(candidate_metadata_path, metadata)
        except Exception:
            shutil.copy2(previous_model, model)
            shutil.copy2(previous_metadata, metadata)
            raise
        return {
            "status": "RETRAINED_FOR_NEXT_CAMPAIGN",
            "eligible_rows": current_count,
            "new_eligible_rows": new_count,
            "training_rows": len(training_rows),
            "model_version": saved["model_version"],
            "model_sha256": saved["model_sha256"],
            "previous_model": str(previous_model),
            "previous_metadata": str(previous_metadata),
        }
