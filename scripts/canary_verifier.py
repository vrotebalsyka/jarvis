#!/usr/bin/env python3
"""Independent fresh HA readback. No model, resolver, executor or renderer."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import home_assistant_inventory as graph
import home_assistant_read as reader
from canary_contract import (
    CanaryError, CanaryRecord, ControlConfig, SealedActionPlan, binding_fingerprint,
)


STABILITY_INTERVAL = 0.20


@dataclass(frozen=True, slots=True)
class Observation:
    started_at: float
    completed_at: float
    states: tuple[tuple[str, str], ...]

    def state(self, canary_id: str) -> str:
        return dict(self.states).get(canary_id, "unavailable")


@dataclass(frozen=True, slots=True)
class Verification:
    verified: bool
    critical: str | None
    after: Observation
    stable_after: Observation
    evidence: tuple[str, ...]
    verified_at: float | None


class CanaryVerifier:
    def __init__(self, read_config: reader.AdapterConfig,
                 *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._config, self._clock, self._sleep = read_config, clock, sleep

    def metadata(self) -> tuple[dict, float]:
        started = self._clock()
        try:
            return graph.collect_inventory(self._config), started
        except (graph.InventoryError, reader.AdapterError):
            raise CanaryError("fresh_metadata_unavailable") from None

    def read(self, records: tuple[CanaryRecord, ...]) -> Observation:
        started = self._clock()
        raw = reader.request_json(self._config, "/api/states")
        if not isinstance(raw, list):
            raise CanaryError("verification_read_failed")
        by_id = {}
        for row in raw:
            if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str):
                raise CanaryError("verification_read_failed")
            if row["entity_id"] in by_id:
                raise CanaryError("verification_read_failed")
            attributes = row.get("attributes", {})
            by_id[row["entity_id"]] = (None if isinstance(attributes, dict) and attributes.get("assumed_state")
                                       else row.get("state"))
        values = tuple((record.canary_id, value if isinstance(value, str) and value in {"on", "off"} else "unavailable")
                       for record in records for value in [by_id.get(record.entity_id)])
        return Observation(started, self._clock(), values)

    def finish(self, plan: SealedActionPlan, config: ControlConfig, before: Observation) -> Verification:
        record = config.record(plan.resolved_target_ref)
        after = self.read(config.records)
        # Do not let a later read/metadata failure erase already observed danger.
        # Return immediately so the adapter persists emergency-OFF before any
        # further verification work. An empty stability observation is not proof.
        def other_changed(observation: Observation) -> bool:
            return any(other.canary_id != record.canary_id
                       and observation.state(other.canary_id) != before.state(other.canary_id)
                       for other in config.records)
        critical = None
        if before.completed_at > after.started_at:
            critical = "verifier_contradiction"
        if other_changed(after):
            critical = "unexpected_canary_change"
        if critical:
            return Verification(False, critical, after, Observation(0, 0, ()), (), None)
        self._sleep(STABILITY_INTERVAL)
        stable = self.read(config.records)
        expected = "on" if plan.action == "turn_on" else "off"
        if (after.completed_at > stable.started_at or after.started_at >= stable.started_at
                or after.state(record.canary_id) == expected and stable.state(record.canary_id) != expected):
            critical = "verifier_contradiction"
        if other_changed(stable):
            critical = "unexpected_canary_change"
        if critical:
            return Verification(False, critical, after, stable, (), None)
        fresh, _started = self.metadata()
        try:
            if binding_fingerprint(fresh, record, plan.action) != plan.target_fingerprint:
                critical = "identity_changed"
            for other in config.records:
                # Revalidate all approved identities, not just the one acted on.
                binding_fingerprint(fresh, other, other.allowed_actions[0])
        except CanaryError:
            critical = "identity_changed"
        verified = (critical is None and after.state(record.canary_id) == expected
                    and stable.state(record.canary_id) == expected)
        return Verification(verified, critical, after, stable,
                            ("independent_after_get", "independent_stability_get", "fresh_registry_identity")
                            if verified else (), time.time() if verified else None)
