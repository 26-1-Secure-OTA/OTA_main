# OTA failure-risk scheduler v1

This package ranks only Secondary ECUs already approved by the existing Safety
Policy. It cannot change ALLOW/HOLD/BLOCK, targets, slots, versions, metadata,
hashes, UIDs, or policy thresholds. The production default is the fixed order
`001 -> 002 -> 003`; AI is requested only with `OTA_SCHEDULER=AI`.

The v1 model has exactly six inputs defined in `feature_schema_v1.json`.
Telemetry validity is a gate, not a model feature. Dataset rows are grouped by
`scenario_id`, never randomly split by row. Training refuses data without both
labels or without at least three groups and never creates a dummy model.

Fault injection is disabled unless `OTA_ENABLE_FAULT_INJECTION=1` and a
supported `OTA_SCENARIO_ID` are both present. Hooks run only after Policy ALLOW.

Dependency scheduling is out of scope for v1. A future implementation must
first compute dependency-eligible ECUs and only then risk-rank that set.

Potential v2 features (not implemented): link response median/jitter, recent
timeout rate, UART error rate, and image-size ratio. `recent_retry_rate` is not
eligible while the Serial protocol has no actual retry mechanism.
