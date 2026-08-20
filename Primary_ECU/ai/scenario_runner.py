import argparse
import os
import subprocess

from .dataset_builder import SCENARIO_RE


def run_scenario(scenario_id: str, command: list[str], *, fault_injection=False) -> int:
    if not SCENARIO_RE.fullmatch(scenario_id):
        raise ValueError(f"invalid scenario_id: {scenario_id}")
    environment = os.environ.copy()
    environment["OTA_SCENARIO_ID"] = scenario_id
    if fault_injection:
        environment["OTA_ENABLE_FAULT_INJECTION"] = "1"
    else:
        environment.pop("OTA_ENABLE_FAULT_INJECTION", None)
    return subprocess.run(command, env=environment, check=False).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario_id")
    parser.add_argument("--fault-injection", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a command is required")
    raise SystemExit(run_scenario(args.scenario_id, args.command, fault_injection=args.fault_injection))


if __name__ == "__main__":
    main()
