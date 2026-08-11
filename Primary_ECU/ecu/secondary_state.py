from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SecondaryStateStore:
    def __init__(
        self,
        state_path: str = "./meta/secondary_states.json",
        log_path: str = "./logs/secondary_update.jsonl",
    ):
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.states = self._load_states()

    def _load_states(self) -> dict:
        if not self.state_path.exists():
            return {}

        try:
            data = json.loads(
                self.state_path.read_text(encoding="utf-8")
            )
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _version_from_artifact(
        artifact: Optional[str],
    ) -> Optional[str]:
        if not artifact:
            return None

        match = re.search(
            r"_(\d+(?:\.\d+)*)(?:_slot[-_][ab])?\.[^.]+$",
            Path(artifact).name,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    def transition(
        self,
        ecu_serial: str,
        new_state: str,
        *,
        artifact: Optional[str] = None,
        firmware_sha256: Optional[str] = None,
        ok: Optional[bool] = None,
        reason: Optional[str] = None,
        retry_count: int = 0,
        details: Optional[dict] = None,
    ) -> None:
        now = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        previous_state = self.states.get(
            ecu_serial, {}
        ).get("state", "UNKNOWN")

        event = {
            "time": now,
            "ecu_serial": ecu_serial,
            "previous_state": previous_state,
            "new_state": new_state,
            "firmware_version": self._version_from_artifact(
                artifact
            ),
            "firmware_sha256": firmware_sha256,
            "artifact": artifact,
            "ok": ok,
            "reason": reason,
            "retry_count": retry_count,
            "details": details or {},
        }

        self.states[ecu_serial] = {
            "ecu_serial": ecu_serial,
            "state": new_state,
            "firmware_version": event["firmware_version"],
            "firmware_sha256": firmware_sha256,
            "artifact": artifact,
            "ok": ok,
            "reason": reason,
            "retry_count": retry_count,
            "updated_at": now,
            "details": details or {},
        }

        self.state_path.parent.mkdir(
            parents=True, exist_ok=True
        )
        self.log_path.parent.mkdir(
            parents=True, exist_ok=True
        )

        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                self.states,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

        print(
            f"[STATE] ECU={ecu_serial} "
            f"{previous_state} -> {new_state}"
        )