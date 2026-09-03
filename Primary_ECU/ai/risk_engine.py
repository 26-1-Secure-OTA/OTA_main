"""Safety-independent risk ordering API with deterministic fallbacks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


FIXED_ORDER = ("stm32-led-001", "stm32-led-002", "stm32-led-003")
MODEL_MODES = frozenset({"OFF", "STATISTICAL", "ISOLATION_FOREST", "SUPERVISED"})
APPLY_MODES = frozenset({"SHADOW", "ACTIVE"})


@dataclass
class RiskResult:
    requested_mode: str
    used_mode: str
    recommended_order: list[str]
    execution_order: list[str]
    scores: dict[str, dict] = field(default_factory=dict)
    model_type: str = "OFF"
    model_version: str | None = None
    model_hash: str | None = None
    fallback_chain: list[dict] = field(default_factory=list)
    fallback_reason: str | None = None
    apply_mode: str = "ACTIVE"
    profile_version: str | None = None
    profile_hash: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_modes(model_mode: str, apply_mode: str | None = None) -> tuple[str, str]:
    """Map legacy OFF/SHADOW/ACTIVE values without breaking callers."""
    requested = str(model_mode).upper()
    if requested in {"SHADOW", "ACTIVE"}:
        applied = requested if apply_mode is None else str(apply_mode).upper()
        return "STATISTICAL", applied
    if requested == "OFF":
        return "OFF", "ACTIVE" if apply_mode is None else str(apply_mode).upper()
    return requested, "ACTIVE" if apply_mode is None else str(apply_mode).upper()


class RiskEngine:
    """Order exactly the supplied ECU set; never make a safety decision."""

    def __init__(
        self,
        *,
        profile_path: str | Path | None = None,
        model_path: str | Path | None = None,
        metadata_path: str | Path | None = None,
        fixed_order=FIXED_ORDER,
    ) -> None:
        self.profile_path = profile_path
        self.model_path = model_path
        self.metadata_path = metadata_path
        self.fixed_order = tuple(fixed_order)

    def _fixed(self, allow_context: Mapping[str, Any]) -> list[str]:
        known = [ecu for ecu in self.fixed_order if ecu in allow_context]
        return known + [ecu for ecu in allow_context if ecu not in self.fixed_order]

    def rank(
        self,
        allow_context: Mapping[str, dict],
        *,
        model_mode: str = "STATISTICAL",
        apply_mode: str | None = None,
    ) -> RiskResult:
        requested_text = str(model_mode).upper()
        model_mode, apply_mode = parse_modes(requested_text, apply_mode)
        fixed = self._fixed(allow_context)
        base = RiskResult(
            requested_mode=requested_text,
            used_mode="OFF",
            recommended_order=list(fixed),
            execution_order=list(fixed),
            model_type="OFF",
            apply_mode=apply_mode,
        )
        if model_mode not in MODEL_MODES:
            base.fallback_reason = "INVALID_AI_MODE"
            return base
        if apply_mode not in APPLY_MODES:
            base.fallback_reason = "INVALID_APPLY_MODE"
            return base
        if model_mode == "OFF" or not fixed:
            return base

        attempts = [model_mode]
        if model_mode in {"ISOLATION_FOREST", "SUPERVISED"}:
            attempts.append("STATISTICAL")

        for attempt in attempts:
            try:
                result = self._run(attempt, allow_context, fixed)
                result.requested_mode = requested_text
                result.apply_mode = apply_mode
                result.execution_order = (
                    list(result.recommended_order) if apply_mode == "ACTIVE" else list(fixed)
                )
                result.fallback_chain = list(base.fallback_chain)
                result.fallback_reason = (
                    base.fallback_chain[0]["reason"] if base.fallback_chain else None
                )
                self._validate_ecu_set(result, fixed)
                return result
            except Exception as exc:  # AI errors must not escape into OTA orchestration.
                reason = self._reason(exc, attempt)
                base.fallback_chain.append({"mode": attempt, "reason": reason})

        base.fallback_reason = base.fallback_chain[0]["reason"]
        return base

    def _run(self, mode, allow_context, fixed):
        if mode == "STATISTICAL":
            from .statistical_engine import StatisticalEngine
            return StatisticalEngine(self.profile_path, self.fixed_order).rank(allow_context)
        if mode == "ISOLATION_FOREST":
            from .isolation_forest_engine import IsolationForestEngine
            return IsolationForestEngine(
                self.model_path, self.metadata_path, self.fixed_order
            ).rank(allow_context)
        if mode == "SUPERVISED":
            from .supervised_engine import SupervisedEngine
            return SupervisedEngine().rank(allow_context)
        raise ValueError("INVALID_AI_MODE")

    @staticmethod
    def _reason(exc: Exception, mode: str) -> str:
        reason = str(exc)
        if reason and len(reason) < 160:
            return reason
        return f"{mode}_EXCEPTION"

    @staticmethod
    def _validate_ecu_set(result: RiskResult, fixed: list[str]) -> None:
        expected = set(fixed)
        if set(result.recommended_order) != expected or set(result.scores) != expected:
            raise ValueError("OUTPUT_ECU_SET_MISMATCH")
