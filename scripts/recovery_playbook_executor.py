#!/usr/bin/env python3
"""Deterministic execution gate for declarative recovery playbooks."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Iterable, Mapping


sys.dont_write_bytecode = True

import recovery_playbook_registry as registry


OUTCOME_STATUSES = frozenset({"verified", "no_action", "failed", "delivery_unknown"})


class PlaybookExecutionError(RuntimeError):
    """A fixed, secret-free recovery execution failure."""


def staged_qualification() -> registry.QualificationRecord:
    """Safe source default: offline/dry-run evidence never enables live R1."""
    return registry.QualificationRecord(
        stage="dry_run",
        offline_tests_passed=True,
        dry_run_passed=True,
        owner_approved=False,
        controlled_live_passed=False,
        post_enable_observed=False,
        rollback_enabled=True,
    )


def execute(
    playbook_id: str,
    fact_ids: Iterable[str],
    *,
    live: bool,
    qualification: registry.QualificationRecord | None = None,
    explicit_owner_request: bool = False,
    separate_confirmation: bool = False,
    attempt_count: int = 0,
    last_attempt_epoch: int | None = None,
    now: int | None = None,
    adapter_executor: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    playbook = registry.get(playbook_id)
    try:
        facts = registry.validate_fact_ids(fact_ids)
    except registry.PlaybookRegistryError as error:
        raise PlaybookExecutionError("recovery facts are invalid") from error
    evidence = playbook.evidence(facts)
    if evidence is None:
        raise PlaybookExecutionError("playbook trigger evidence is incomplete")
    current = int(time.time()) if now is None else now
    if attempt_count < 0 or attempt_count >= playbook.maximum_attempts:
        return {
            "schema_version": 1, "playbook_id": playbook.playbook_id,
            "mode": "live" if live else "dry_run", "status": "attempts_exhausted",
            "adapter_calls": 0, "evidence_fact_ids": evidence,
        }
    if (
        last_attempt_epoch is not None
        and current < last_attempt_epoch + playbook.cooldown_seconds
    ):
        return {
            "schema_version": 1, "playbook_id": playbook.playbook_id,
            "mode": "live" if live else "dry_run", "status": "cooldown",
            "next_allowed_epoch": last_attempt_epoch + playbook.cooldown_seconds,
            "adapter_calls": 0, "evidence_fact_ids": evidence,
        }
    if not live:
        return {
            "schema_version": 1, "playbook_id": playbook.playbook_id,
            "playbook_version": playbook.version, "mode": "dry_run",
            "status": "planned", "risk_class": playbook.risk_class,
            "adapter_ids": [step.adapter_id for step in playbook.ordered_steps],
            "verification": [step.verification for step in playbook.ordered_steps],
            "rollback": playbook.rollback, "adapter_calls": 0,
            "evidence_fact_ids": evidence,
        }
    if qualification is None or not registry.authorize_live(
        playbook.playbook_id,
        qualification,
        explicit_owner_request=explicit_owner_request,
        separate_confirmation=separate_confirmation,
    ):
        return {
            "schema_version": 1, "playbook_id": playbook.playbook_id,
            "mode": "live", "status": "qualification_required",
            "risk_class": playbook.risk_class, "adapter_calls": 0,
            "evidence_fact_ids": evidence,
        }
    if adapter_executor is None:
        raise PlaybookExecutionError("allow-listed adapter executor is unavailable")
    outcomes: list[dict[str, Any]] = []
    calls = 0
    for step in playbook.ordered_steps:
        context = {
            "playbook_id": playbook.playbook_id,
            "playbook_version": playbook.version,
            "step_order": step.order,
            "expected_verification": step.verification,
            "evidence_fact_ids": evidence,
        }
        raw = adapter_executor(step.adapter_id, context)
        calls += 1
        if not isinstance(raw, Mapping) or set(raw) != {
            "status", "verification", "changed"
        }:
            raise PlaybookExecutionError("recovery adapter result is invalid")
        status = raw.get("status")
        verification = raw.get("verification")
        changed = raw.get("changed")
        if (
            status not in OUTCOME_STATUSES or not isinstance(verification, str)
            or not isinstance(changed, bool) or len(verification) > 160
        ):
            raise PlaybookExecutionError("recovery adapter result is invalid")
        outcome = {
            "order": step.order, "adapter_id": step.adapter_id,
            "status": status, "verification": verification, "changed": changed,
        }
        outcomes.append(outcome)
        # A verified/no-action result is a stop condition.  A definite failure
        # may fall through to the next explicitly declared safe step.  An
        # unknown delivery must stop immediately so an action is never repeated.
        if status in {"verified", "no_action", "delivery_unknown"}:
            break
    completed = bool(outcomes) and outcomes[-1]["status"] in {
        "verified", "no_action"
    }
    return {
        "schema_version": 1, "playbook_id": playbook.playbook_id,
        "playbook_version": playbook.version, "mode": "live",
        "status": "verified" if completed else "stopped",
        "risk_class": playbook.risk_class, "adapter_calls": calls,
        "step_outcomes": outcomes, "rollback": playbook.rollback,
        "evidence_fact_ids": evidence,
        "stop_reason": "completed" if completed else outcomes[-1]["status"],
    }
