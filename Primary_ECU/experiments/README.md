# STM32 fault-injection experiments

This directory is owned by Work Package C. It configures hardware scenarios;
Safety decisions, failure taxonomy, labels, and the OTA installer remain owned
by Work Package A.

## Wire contract

```text
Primary -> FAULT_SET,<SCENARIO>,<PARAMETER>,<VALUE>[,<ATTEMPT_ID>]
STM32   -> FAULT_ACK,<SCENARIO>
```

`ATTEMPT_ID` is an optional eight-digit hexadecimal correlation token. STATUS
keeps all existing fields and adds:

```text
RESET_CONTEXT=<NONE|OTA_ACTIVATION|TRANSFER|BOOT_TEST>
BOOT_ID=<0..65535>
SCENARIO=<SCENARIO>
ATTEMPT_ID=<8 hex digits>
```

`NORMAL` clears the test fault. Reset/boot scenarios are persisted beside the
existing boot flag so the next application can report them. `BOOT_FAILED`
always falls back to the previous valid slot instead of leaving the board
unbootable.

## Runner

Preview the default 20-campaign/60-attempt matrix without hardware:

```bash
python3 experiments/scenario_runner.py --dry-run
```

For hardware runs, provide the normal OTA entrypoint as a command:

```bash
python3 experiments/scenario_runner.py \
  --ota-command "python3 experiments/ota_once.py"
```

`ota_once.py` selects the inactive-slot image matching the board's currently
running version from `STM32_Workspace/firmware`. Same-version reinstall is
enabled only when the runner supplies a scenario, campaign ID, and valid
eight-digit attempt ID.

The command receives `OTA_SCENARIO_ID`, `OTA_CAMPAIGN_ID`, `OTA_ATTEMPT_ID`,
and `OTA_SECONDARY_ID`. Its final non-empty stdout line must be a JSON object
containing `"preflight_decision": "ALLOW"`. Expected OTA failures may still
return a normal JSON result; the runner always clears the fault and verifies
that STATUS returns to `NORMAL` before the next attempt.

After Work Package A's experiment logger has produced the training JSONL:

```bash
python3 experiments/validate_dataset.py logs/ota_experiments.jsonl
```

The validator requires at least 60 unique, attempted, preflight-ALLOW rows and
requires both successful and failed outcomes.
