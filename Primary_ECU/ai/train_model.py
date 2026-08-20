import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .dataset_builder import DatasetValidationError, build_training_rows, group_split, read_jsonl, split_summary
from .feature_schema import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FEATURE_UNITS
from .model_loader import sha256_file


def train(log_path, model_path, metadata_path):
    rows, errors = read_jsonl(log_path)
    if errors:
        raise DatasetValidationError("log validation failed:\n" + "\n".join(errors))
    training_rows, rejected = build_training_rows(rows)
    labels = {row["failure_label"] for row in training_rows}
    if labels != {0, 1}:
        raise DatasetValidationError(f"both success and failure labels are required; got {sorted(labels)}")
    splits = group_split(training_rows)
    try:
        import joblib
        from sklearn.metrics import balanced_accuracy_score, classification_report, confusion_matrix
        from sklearn.tree import DecisionTreeClassifier, export_graphviz, export_text
    except ImportError as exc:
        raise DatasetValidationError("scikit-learn and joblib are required for training") from exc
    train_rows = splits["train"]
    if {row["failure_label"] for row in train_rows} != {0, 1}:
        raise DatasetValidationError("training split must contain both labels")
    model = DecisionTreeClassifier(max_depth=3, min_samples_leaf=5, class_weight="balanced", random_state=42)
    model.fit([row["features"] for row in train_rows], [row["failure_label"] for row in train_rows])
    metrics = {"splits": split_summary(splits), "feature_importance": dict(zip(FEATURE_NAMES, model.feature_importances_.tolist())), "decision_tree": export_text(model, feature_names=list(FEATURE_NAMES))}
    for split_name in ("validation", "test"):
        split = splits[split_name]
        expected = [row["failure_label"] for row in split]
        predicted = model.predict([row["features"] for row in split])
        metrics[split_name] = {"confusion_matrix": confusion_matrix(expected, predicted, labels=[0, 1]).tolist(), "balanced_accuracy": balanced_accuracy_score(expected, predicted), "classification_report": classification_report(expected, predicted, labels=[0, 1], output_dict=True, zero_division=0)}
    model_path, metadata_path = Path(model_path), Path(metadata_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path)
    visualization_path = model_path.with_suffix(".dot")
    export_graphviz(
        model, out_file=str(visualization_path), feature_names=list(FEATURE_NAMES),
        class_names=["success", "failure"], filled=True, rounded=True,
    )
    metadata = {
        "model_version": "decision-tree-v1", "model_hash": sha256_file(model_path),
        "feature_schema_version": FEATURE_SCHEMA_VERSION, "feature_names": list(FEATURE_NAMES),
        "feature_order": list(FEATURE_NAMES), "feature_units": FEATURE_UNITS,
        "training_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "metrics": metrics, "decision_tree_visualization": str(visualization_path),
        "rejected_training_candidates": rejected,
    }
    with metadata_path.open("w", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2, ensure_ascii=False)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log_path")
    parser.add_argument("--model", default="models/decision-tree-v1.joblib")
    parser.add_argument("--metadata", default="models/decision-tree-v1.metadata.json")
    args = parser.parse_args()
    print(json.dumps(train(args.log_path, args.model, args.metadata), indent=2))


if __name__ == "__main__":
    main()
