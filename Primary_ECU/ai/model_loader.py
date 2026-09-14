"""Validated loading of serialized AI pipelines and their metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import sklearn

from .feature_schema import FeatureSchema, FeatureValidationError


class ModelLoadError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_model(
    model_path: str | Path,
    metadata_path: str | Path,
    *,
    expected_type: str,
    expected_schema_version: int = 1,
):
    model_path, metadata_path = Path(model_path), Path(metadata_path)
    if not model_path.is_file():
        raise ModelLoadError("MODEL_NOT_FOUND")
    if not metadata_path.is_file():
        raise ModelLoadError("METADATA_NOT_FOUND")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelLoadError("METADATA_INVALID") from exc
    if not isinstance(metadata, dict):
        raise ModelLoadError("METADATA_INVALID")
    if metadata.get("model_sha256") != sha256_file(model_path):
        raise ModelLoadError("MODEL_HASH_MISMATCH")
    if metadata.get("model_type") != expected_type:
        raise ModelLoadError("MODEL_TYPE_MISMATCH")
    if not isinstance(metadata.get("model_version"), str):
        raise ModelLoadError("MODEL_VERSION_INVALID")
    if metadata.get("feature_schema_version") != expected_schema_version:
        raise ModelLoadError("FEATURE_SCHEMA_MISMATCH")
    try:
        FeatureSchema(expected_schema_version).validate_feature_order(metadata.get("feature_order", ()))
    except FeatureValidationError as exc:
        raise ModelLoadError(str(exc)) from exc

    trained_version = str(metadata.get("sklearn_version") or "")
    if trained_version.split(".")[:2] != sklearn.__version__.split(".")[:2]:
        raise ModelLoadError("SKLEARN_VERSION_MISMATCH")
    try:
        model = joblib.load(model_path)
    except Exception as exc:
        raise ModelLoadError("MODEL_DESERIALIZATION_FAILED") from exc
    if not hasattr(model, "predict"):
        raise ModelLoadError("MODEL_INTERFACE_INVALID")
    return model, metadata

