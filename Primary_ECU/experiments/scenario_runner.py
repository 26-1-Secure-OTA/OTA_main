from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

PRIMARY_ROOT = Path(__file__).resolve().parents[1]
ECU_ROOT = PRIMARY_ROOT / "ecu"
if str(ECU_ROOT) not in sys.path:
    sys.path.insert(0, str(ECU_ROOT))

from secondary_serial import (  # noqa: E402
    FAULT_SCENARIO_DEFAULTS,
    SecondarySerial,
)


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    campaign_count: int


# 20 campaigns x 3 boards = 60 preflight-ALLOW attempts. Safety BLOCK
# scenarios remain available through the protocol but are intentionally not
# part of the ML dataset plan.
DEFAULT_SCENARIO_PLAN = (
    ScenarioSpec("NORMAL", 4),
    ScenarioSpec("LINK_DELAY_LOW", 2),
    ScenarioSpec("LINK_DELAY_MEDIUM", 2),
    ScenarioSpec("LINK_DELAY_HIGH_ALLOW", 2),
    ScenarioSpec("VOLTAGE_LOW_ALLOW", 2),
    ScenarioSpec("TEMP_HIGH_ALLOW", 2),
    ScenarioSpec("TRANSFER_TIMEOUT", 1),
    ScenarioSpec("TRANSFER_INTERRUPTED", 1),
    ScenarioSpec("TRANSFER_CORRUPTION", 1),
    ScenarioSpec("RESET_DURING_TRANSFER", 1),
    ScenarioSpec("BOOT_FAILED", 1),
    ScenarioSpec("POST_REBOOT_HEALTH_FAIL", 1),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_attempts(
    boards: Iterable[str],
    plan: Iterable[ScenarioSpec] = DEFAULT_SCENARIO_PLAN,
) -> list[dict]:
    attempts = []
    for spec in plan:
        if spec.name not in FAULT_SCENARIO_DEFAULTS:
            raise ValueError(f"unknown scenario in plan: {spec.name}")
        if spec.campaign_count <= 0:
            raise ValueError("campaign_count must be positive")
        for campaign_index in range(spec.campaign_count):
            campaign_id = (
                f"fault-{spec.name.lower()}-{campaign_index + 1:02d}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            for board in boards:
                attempts.append({
                    "campaign_id": campaign_id,
                    "attempt_id": uuid.uuid4().hex[:8].upper(),
                    "secondary_id": board,
                    "scenario": spec.name,
                })
    return attempts


class CommandOtaExecutor:
    """Run the team's normal OTA entrypoint without coupling to installer.py.

    The command receives experiment context through environment variables. It
    must print one JSON object as its final non-empty stdout line, including
    ``preflight_decision``. Team 1 remains the owner of policy and labels.
    """

    def __init__(self, command: str, timeout_seconds: float = 300.0):
        self.argv = shlex.split(command)
        if not self.argv:
            raise ValueError("ota command must not be empty")
        self.timeout_seconds = timeout_seconds

    def __call__(self, context: dict) -> dict:
        environment = os.environ.copy()
        environment.update({
            "OTA_DATA_SOURCE": "BOARD",
            "OTA_SCENARIO_ID": context["scenario"],
            "OTA_CAMPAIGN_ID": context["campaign_id"],
            "OTA_ATTEMPT_ID": context["attempt_id"],
            "OTA_SECONDARY_ID": context["secondary_id"],
        })
        completed = subprocess.run(
            self.argv,
            cwd=PRIMARY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        output_lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        try:
            result = json.loads(output_lines[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "OTA command must end stdout with a JSON result"
            ) from exc
        result.setdefault("returncode", completed.returncode)
        result.setdefault("stderr", completed.stderr.strip())
        return result


class ScenarioRunner:
    def __init__(
        self,
        ota_executor: Callable[[dict], dict],
        *,
        serial_factory=SecondarySerial,
        discover: Callable[..., dict] = SecondarySerial.discover_secondaries,
        recovery_timeout_seconds: float = 30.0,
        run_log_path: Optional[Path] = None,
    ):
        self.ota_executor = ota_executor
        self.serial_factory = serial_factory
        self.discover = discover
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.run_log_path = run_log_path

    def _port_for(self, secondary_id: str) -> str:
        discovered = self.discover(status_sample_count=1)
        if secondary_id not in discovered:
            raise RuntimeError(f"board not discovered: {secondary_id}")
        return discovered[secondary_id]["port"]

    def _configure(self, context: dict) -> dict:
        port = self._port_for(context["secondary_id"])
        with self.serial_factory(port=port) as secondary:
            ack = secondary.set_fault(
                context["scenario"],
                attempt_id=context["attempt_id"],
            )
            status = secondary.get_status(timeout_seconds=10.0)
        if status.get("scenario") != context["scenario"]:
            raise RuntimeError("STATUS did not echo configured scenario")
        if status.get("attempt_id") != context["attempt_id"]:
            raise RuntimeError("STATUS did not echo configured attempt_id")
        return {"ack": ack, "status": status}

    def _recover(self, secondary_id: str) -> dict:
        deadline = time.monotonic() + self.recovery_timeout_seconds
        last_error = None
        while time.monotonic() < deadline:
            try:
                port = self._port_for(secondary_id)
                with self.serial_factory(port=port) as secondary:
                    secondary.clear_fault(timeout_seconds=5.0)
                    status = secondary.get_status(timeout_seconds=10.0)
                if status.get("scenario") in (None, "NORMAL"):
                    return status
                last_error = RuntimeError("board did not return to NORMAL")
            except Exception as exc:  # hardware reconnects are expected here
                last_error = exc
            time.sleep(1.0)
        raise RuntimeError(
            f"board recovery timed out: {secondary_id}: {last_error}"
        )

    def _append_run_log(self, row: dict) -> None:
        if self.run_log_path is None:
            return
        self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.run_log_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, sort_keys=True) + "\n")

    def run_attempt(self, context: dict) -> dict:
        row = {**context, "started_at": now_iso()}
        error = None
        try:
            configured = self._configure(context)
            row["pre_status"] = configured["status"]
            result = self.ota_executor(context)
            row["ota_result"] = result
            if result.get("preflight_decision") != "ALLOW":
                raise RuntimeError(
                    "ML experiment attempt did not start from preflight ALLOW"
                )
        except Exception as exc:
            error = exc
            row["runner_error"] = str(exc)
        finally:
            try:
                row["recovery_status"] = self._recover(
                    context["secondary_id"]
                )
            except Exception as recovery_exc:
                row["recovery_error"] = str(recovery_exc)
                if error is None:
                    error = recovery_exc
            row["finished_at"] = now_iso()
            self._append_run_log(row)
        if error is not None:
            raise error
        return row

    def run(self, attempts: Iterable[dict]) -> list[dict]:
        return [self.run_attempt(attempt) for attempt in attempts]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the 20-campaign STM32 fault-injection matrix.",
    )
    parser.add_argument(
        "--boards",
        nargs="+",
        default=sorted(SecondarySerial.EXPECTED_SECONDARIES),
    )
    parser.add_argument("--ota-command")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--run-log",
        type=Path,
        default=PRIMARY_ROOT / "logs" / "scenario_runs.jsonl",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    attempts = build_attempts(args.boards)
    if args.dry_run:
        print(json.dumps({
            "campaigns": len({row["campaign_id"] for row in attempts}),
            "attempts": len(attempts),
            "plan": [spec.__dict__ for spec in DEFAULT_SCENARIO_PLAN],
        }, indent=2))
        return 0
    if not args.ota_command:
        raise SystemExit("--ota-command is required unless --dry-run is used")
    runner = ScenarioRunner(
        CommandOtaExecutor(args.ota_command),
        run_log_path=args.run_log,
    )
    runner.run(attempts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
