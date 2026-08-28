"""RiskEngine adapter preserving the original Median/MAD scorer exactly."""

from __future__ import annotations

from .risk_engine import RiskResult


class StatisticalEngine:
    def __init__(self, profile_path, fixed_order) -> None:
        self.profile_path = profile_path
        self.fixed_order = tuple(fixed_order)

    def rank(self, allow_context: dict[str, dict]) -> RiskResult:
        # Imports are local to keep anomaly_scorer as the compatibility surface.
        from .anomaly_scorer import load_profile, score_secondary

        profile, profile_hash = load_profile(self.profile_path)
        fixed = [ecu for ecu in self.fixed_order if ecu in allow_context]
        fixed += [ecu for ecu in allow_context if ecu not in self.fixed_order]
        scores = {}
        for secondary_id in fixed:
            context = allow_context[secondary_id]
            status = context.get("status") or {}
            if status.get("telemetry_valid") is not True or status.get("telemetry_valid_all") is not True:
                raise ValueError("INVALID_TELEMETRY")
            scores[secondary_id] = score_secondary(
                secondary_id=secondary_id,
                uid=status.get("uid"),
                features=context.get("features") or {},
                profile=profile,
            )
        tie_break = {ecu: i for i, ecu in enumerate(fixed)}
        recommended = sorted(fixed, key=lambda ecu: (scores[ecu]["risk_score"], tie_break[ecu]))
        return RiskResult(
            requested_mode="STATISTICAL",
            used_mode="STATISTICAL",
            recommended_order=recommended,
            execution_order=recommended,
            scores=scores,
            model_type="STATISTICAL",
            model_version=profile.get("profile_version"),
            model_hash=profile_hash,
            profile_version=profile.get("profile_version"),
            profile_hash=profile_hash,
        )

