import hashlib
import json
from pathlib import Path

from .feature_schema import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FEATURE_UNITS


class ModelValidationError(ValueError):
    pass


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def validate_metadata(metadata: dict, model_path) -> None:
    required = {
        "model_version", "model_hash", "feature_schema_version",
        "feature_names", "feature_order", "feature_units", "training_timestamp",
    }
    missing = sorted(required - metadata.keys())
    if missing:
        raise ModelValidationError(f"model metadata missing: {missing}")
    if metadata["model_hash"] != sha256_file(model_path):
        raise ModelValidationError("MODEL_HASH_MISMATCH")
    if metadata["feature_schema_version"] != FEATURE_SCHEMA_VERSION:
        raise ModelValidationError("FEATURE_SCHEMA_MISMATCH")
    if tuple(metadata["feature_names"]) != FEATURE_NAMES or tuple(metadata["feature_order"]) != FEATURE_NAMES:
        raise ModelValidationError("FEATURE_SCHEMA_MISMATCH")
    if metadata["feature_units"] != FEATURE_UNITS:
        raise ModelValidationError("FEATURE_SCHEMA_MISMATCH")


def load_model(model_path, metadata_path):
    model_path, metadata_path = Path(model_path), Path(metadata_path)
    if not model_path.is_file() or not metadata_path.is_file():
        raise ModelValidationError("MODEL_NOT_FOUND")
    with metadata_path.open("r", encoding="utf-8") as source:
        metadata = json.load(source)
    validate_metadata(metadata, model_path)
    try:
        import joblib
    except ImportError as exc:
        raise ModelValidationError("MODEL_DEPENDENCY_MISSING") from exc
    return joblib.load(model_path), metadata
