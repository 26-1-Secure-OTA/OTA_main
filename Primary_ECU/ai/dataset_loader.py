"""Strict JSONL-to-matrix loader for aggregated normal STATUS rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .feature_schema import DEFAULT_SCHEMA, FeatureSchema, FeatureValidationError


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray
    rows: list[dict]
    source_sha256: str
    feature_order: tuple[str, ...]


def load_jsonl(path: str | Path, schema: FeatureSchema = DEFAULT_SCHEMA) -> Dataset:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise DatasetError("DATASET_NOT_FOUND") from exc
    vectors, rows = [], []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError
            vectors.append(schema.to_vector(row.get("ai_features")))
        except (json.JSONDecodeError, TypeError, FeatureValidationError) as exc:
            raise DatasetError(f"INVALID_DATASET_ROW:{line_number}:{exc}") from exc
        rows.append(row)
    if not rows:
        raise DatasetError("DATASET_EMPTY")
    return Dataset(
        X=np.asarray(vectors, dtype=float),
        rows=rows,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        feature_order=schema.feature_order,
    )

