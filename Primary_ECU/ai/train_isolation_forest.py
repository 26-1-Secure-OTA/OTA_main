"""Train the reproducible v1 Isolation Forest on aggregated normal rows."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler

from .dataset_loader import load_jsonl
from .feature_schema import DEFAULT_SCHEMA
from .model_loader import sha256_file


PRIMARY_ECU_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PRIMARY_ECU_DIR / "data" / "ai_baseline" / "run30" / "normal_status_aggregated.jsonl"
DEFAULT_MODEL = PRIMARY_ECU_DIR / "models" / "isolation-forest-v1.joblib"
DEFAULT_METADATA = PRIMARY_ECU_DIR / "models" / "isolation-forest-v1.metadata.json"
MODEL_VERSION = "isolation-forest-v1"
RANDOM_STATE = 42


def train(dataset_path=DEFAULT_INPUT):
    dataset = load_jsonl(dataset_path, DEFAULT_SCHEMA)
    pipeline = Pipeline([
        ("scaler", RobustScaler()),
        ("model", IsolationForest(
            n_estimators=100,
            max_samples="auto",
            contamination="auto",
            random_state=RANDOM_STATE,
            n_jobs=1,
        )),
    ])
    pipeline.fit(dataset.X)
    anomaly = -pipeline.score_samples(dataset.X)
    quantiles = np.quantile(anomaly, np.linspace(0.0, 1.0, 101)).tolist()
    metadata = {
        "model_type": "ISOLATION_FOREST",
        "model_version": MODEL_VERSION,
        "feature_schema_version": DEFAULT_SCHEMA.version,
        "feature_order": list(DEFAULT_SCHEMA.feature_order),
        "training_rows": len(dataset.rows),
        "training_source_sha256": dataset.source_sha256,
        "sklearn_version": sklearn.__version__,
        "random_state": RANDOM_STATE,
        "parameters": {
            "n_estimators": 100,
            "max_samples": "auto",
            "contamination": "auto",
        },
        "risk_normalization": {
            "method": "training_normal_empirical_percentile",
            "direction": "higher_is_more_anomalous",
            "quantile_probabilities": np.linspace(0.0, 1.0, 101).tolist(),
            "anomaly_score_quantiles": quantiles,
        },
    }
    return pipeline, metadata


def save_artifacts(pipeline, metadata, model_path=DEFAULT_MODEL, metadata_path=DEFAULT_METADATA):
    model_path, metadata_path = Path(model_path), Path(metadata_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_model = model_path.with_suffix(model_path.suffix + ".tmp")
    joblib.dump(pipeline, temporary_model, compress=3)
    os.replace(temporary_model, model_path)
    metadata = dict(metadata, model_sha256=sha256_file(model_path))
    temporary_metadata = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    temporary_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary_metadata, metadata_path)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    args = parser.parse_args()
    pipeline, metadata = train(args.input)
    metadata = save_artifacts(pipeline, metadata, args.model, args.metadata)
    print(f"trained rows={metadata['training_rows']} model={args.model} sha256={metadata['model_sha256']}")


if __name__ == "__main__":
    main()

