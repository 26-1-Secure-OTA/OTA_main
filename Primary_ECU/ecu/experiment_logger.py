import json
from datetime import datetime, timezone
from pathlib import Path


class ExperimentLogger:
    def __init__(
        self,
        log_path: str = "./logs/ota_experiments.jsonl",
    ):
        self.log_path = Path(log_path)

    @staticmethod
    def now_iso() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def append(self, row: dict) -> None:
        self.log_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output = dict(row)
        output.setdefault(
            "collected_at",
            self.now_iso(),
        )

        with self.log_path.open(
            "a",
            encoding="utf-8",
        ) as log:
            log.write(
                json.dumps(
                    output,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )