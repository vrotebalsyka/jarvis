#!/usr/bin/env python3
"""One bounded light/switch adapter. Public execution accepts only a sealed plan."""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

import home_assistant_read as reader
from canary_contract import (
    STATE_PATH, MAX_METADATA_AGE, CanaryError, ControlConfig, SealedActionPlan,
    binding_fingerprint, digest, load_config, valid_plan,
)
from canary_verifier import CanaryVerifier, Observation


_PATHS = {
    ("light", "turn_on"): "/api/services/light/turn_on",
    ("light", "turn_off"): "/api/services/light/turn_off",
    ("switch", "turn_on"): "/api/services/switch/turn_on",
    ("switch", "turn_off"): "/api/services/switch/turn_off",
}
_STATUSES = frozenset({"not_sent", "rejected", "failed", "delivery_unknown", "accepted_unverified", "verified"})


@dataclass(frozen=True, slots=True, repr=False)
class ActionReceipt:
    plan_id: str
    status: str
    target_ref: str
    action: str
    service_calls: int = 0
    before: tuple[tuple[str, str], ...] = ()
    after: tuple[tuple[str, str], ...] = ()
    stable_after: tuple[tuple[str, str], ...] = ()
    verification_profile: str = "stable_boolean_state"
    verification_evidence: tuple[str, ...] = ()
    verified_at: float | None = None
    reason: str = ""
    replayed: bool = False
    no_op: bool = False
    post_s: float = 0.0
    verification_s: float = 0.0
    total_s: float = 0.0
    before_read_at: float = 0.0


class _Journal:
    """Global process lock plus durable pre-send reservation; not dialog memory."""
    def __init__(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise CanaryError("unsafe_control_state")
        self.directory = directory
        self.lock = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            path = directory / "plans.sqlite3"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            os.close(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
                raise CanaryError("unsafe_control_state")
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, request_key TEXT UNIQUE, signature TEXT, receipt TEXT)")
            self.db.commit()
        except Exception:
            os.close(self.lock)
            raise

    def close(self) -> None:
        self.db.close()
        os.close(self.lock)

    def stopped(self) -> bool:
        return (self.directory / "emergency-stop.json").exists()

    def stop(self, reason: str) -> None:
        # A sticky OFF override for both owner flags. Only an owner may remove it.
        payload = json.dumps({"CONTROL_ENABLED": False, "CANARY_LIVE_ENABLED": False,
                              "reason": reason, "at": time.time()}).encode()
        fd = os.open(self.directory / "emergency-stop.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def reserve(self, plan: SealedActionPlan) -> ActionReceipt | None:
        signature = digest([plan.intent_hash, plan.resolved_target_ref, plan.action])
        prior = self.db.execute("SELECT signature, receipt FROM plans WHERE id=? OR request_key=?",
                                (plan.plan_id, plan.idempotency_key)).fetchone()
        if prior:
            if prior[0] != signature:
                self.stop("duplicate_request_contradiction")
                raise CanaryError("duplicate_request_contradiction")
            if prior[1]:
                values = json.loads(prior[1])
                for key in ("before", "after", "stable_after"):
                    values[key] = tuple(tuple(item) for item in values[key])
                values["verification_evidence"] = tuple(values["verification_evidence"])
                return replace(ActionReceipt(**values), replayed=True, service_calls=0)
            return ActionReceipt(plan.plan_id, "delivery_unknown", plan.resolved_target_ref,
                                 plan.action, reason="previous_attempt_unresolved", replayed=True)
        self.db.execute("INSERT INTO plans VALUES (?,?,?,NULL)",
                        (plan.plan_id, plan.idempotency_key, signature))
        self.db.commit()  # Must reach durable storage before any POST.
        return None

    def finish(self, receipt: ActionReceipt) -> None:
        self.db.execute("UPDATE plans SET receipt=? WHERE id=?",
                        (json.dumps(asdict(receipt)), receipt.plan_id))
        self.db.commit()

    def receipt(self, plan_id: str) -> ActionReceipt | None:
        row = self.db.execute("SELECT receipt FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row or not row[0]:
            return None
        values = json.loads(row[0])
        for key in ("before", "after", "stable_after"):
            values[key] = tuple(tuple(item) for item in values[key])
        values["verification_evidence"] = tuple(values["verification_evidence"])
        return ActionReceipt(**values)

    def unresolved(self, except_id: str) -> bool:
        return any(raw is None or json.loads(raw)["status"] in {"delivery_unknown", "accepted_unverified"}
                   for (raw,) in self.db.execute("SELECT receipt FROM plans WHERE id != ?", (except_id,)))


def _action_token() -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise CanaryError("action_credential_missing")
    try:
        raw = reader._read_private_file(Path(directory) / "home-assistant-action.token", 4096)
        token = raw.decode().strip()
        if not reader.TOKEN_RE.fullmatch(token):
            raise CanaryError("action_credential_invalid")
        return token
    except (OSError, UnicodeError, reader.AdapterError):
        raise CanaryError("action_credential_unavailable") from None


def _connection() -> http.client.HTTPConnection:
    # No URL, host, path or service argument exists on the public adapter.
    return http.client.HTTPConnection(reader.EXPECTED_HOST, reader.EXPECTED_PORT, timeout=0.8)


class CanaryWriteAdapter:
    def __init__(self, verifier: CanaryVerifier, *, config_loader: Callable[[], ControlConfig] = load_config,
                 state_path: Path = STATE_PATH, token_loader: Callable[[], str] = _action_token) -> None:
        self._verifier, self._config_loader = verifier, config_loader
        self._state_path, self._token_loader = state_path, token_loader

    def emergency_stop(self) -> None:
        """Host/oracle safety stop only. No network or automatic rollback."""
        journal = _Journal(self._state_path)
        try:
            journal.stop("owner_or_oracle_stop")
        finally:
            journal.close()

    def verify_pending(self, plan: SealedActionPlan) -> ActionReceipt:
        """Read-only continuation of an already sent plan. Never calls execute.

        Execution TTL may have elapsed; authentication of the original seal and
        its durable receipt is still mandatory. No action token is loaded.
        """
        rejected = ActionReceipt("", "rejected", "", "", reason="invalid_pending_plan")
        if type(plan) is not SealedActionPlan or not valid_plan(plan, now=plan.created_at):
            return rejected
        journal = _Journal(self._state_path)
        try:
            receipt = journal.receipt(plan.plan_id)
            if receipt is None or (receipt.target_ref, receipt.action) != (plan.resolved_target_ref, plan.action):
                return rejected
            if receipt.status not in {"accepted_unverified", "delivery_unknown"}:
                return replace(receipt, replayed=True, service_calls=0)
            config = self._config_loader()
            if journal.stopped() or not config.enabled:
                return replace(receipt, service_calls=0, reason="control_disabled")
            if config.fingerprint != plan.config_fingerprint:
                journal.stop("allowlist_changed")
                return replace(receipt, service_calls=0, reason="allowlist_changed")
            if time.time() > plan.created_at + 30:
                return replace(receipt, service_calls=0, reason="verification_window_elapsed")
            started = time.monotonic()
            try:
                proof = self._verifier.finish(plan, config,
                    Observation(receipt.before_read_at, receipt.before_read_at, receipt.before))
                receipt = replace(receipt, after=proof.after.states, stable_after=proof.stable_after.states,
                                  verification_s=receipt.verification_s + time.monotonic() - started)
                if proof.critical:
                    journal.stop(proof.critical)
                    receipt = replace(receipt, status="failed", reason=proof.critical)
                elif proof.verified:
                    receipt = replace(receipt, status="verified", verified_at=proof.verified_at,
                                      verification_evidence=proof.evidence, reason="")
                journal.finish(receipt)
            except (CanaryError, reader.AdapterError, OSError, ValueError, KeyError):
                pass  # Unknown remains unknown; subsequent continuation is GET-only.
            return replace(receipt, service_calls=0)
        finally:
            journal.close()

    def _validate(self, plan: SealedActionPlan, config: ControlConfig, journal: _Journal) -> None:
        if not valid_plan(plan):
            raise CanaryError("invalid_or_expired_plan")
        if journal.stopped() or not config.enabled:
            raise CanaryError("control_disabled")
        if config.fingerprint != plan.config_fingerprint:
            raise CanaryError("allowlist_changed")
        record = config.record(plan.resolved_target_ref)
        if (plan.action not in record.allowed_actions or plan.resolved_domain != record.domain
                or plan.resolved_area != record.control_area
                or plan.registry_area != record.registry_area or plan.owner_area != record.owner_area
                or plan.verification_profile != record.verification_profile
                or plan.rollback_action not in record.rollback_actions):
            raise CanaryError("plan_policy_mismatch")

    def execute(self, plan: SealedActionPlan) -> ActionReceipt:
        if type(plan) is not SealedActionPlan:
            return ActionReceipt("", "rejected", "", "", reason="malformed_plan")
        receipt = ActionReceipt(plan.plan_id, "rejected", plan.resolved_target_ref, plan.action)
        started = time.monotonic()
        journal = None
        reserved = False
        try:
            if not valid_plan(plan):
                return replace(receipt, reason="invalid_or_expired_plan")
            config = self._config_loader()
            if not config.enabled:
                return replace(receipt, status="not_sent", reason="control_disabled")
            journal = _Journal(self._state_path)
            self._validate(plan, config, journal)
            replay = journal.reserve(plan)
            if replay is not None:
                return replay
            reserved = True
            if journal.unresolved(plan.plan_id):
                raise CanaryError("previous_action_unresolved")
            record = config.record(plan.resolved_target_ref)
            fresh, observed = self._verifier.metadata()
            try:
                fingerprint = binding_fingerprint(fresh, record, plan.action)
                for other in config.records:
                    binding_fingerprint(fresh, other, other.allowed_actions[0])
            except (CanaryError, KeyError, TypeError, ValueError):
                journal.stop("identity_changed")
                raise CanaryError("identity_changed") from None
            if fingerprint != plan.target_fingerprint:
                journal.stop("fingerprint_changed")
                raise CanaryError("fingerprint_changed")
            before = self._verifier.read(config.records)
            if any(value not in {"on", "off"} for _, value in before.states):
                raise CanaryError("before_state_unavailable")
            receipt = replace(receipt, before=before.states, before_read_at=before.completed_at)
            # Recheck immediately before send, including a newly loaded owner config.
            self._validate(plan, self._config_loader(), journal)
            if time.monotonic() - observed > MAX_METADATA_AGE:
                raise CanaryError("stale_inventory")
            desired = "on" if plan.action == "turn_on" else "off"
            no_op = before.state(record.canary_id) == desired
            if no_op and not record.allow_noop:
                raise CanaryError("noop_not_approved")
            if no_op:
                receipt = replace(receipt, status="accepted_unverified", no_op=True)
            else:
                token = self._token_loader()
                if token == self._verifier._config.token:
                    raise CanaryError("action_credential_must_be_separate")
                path = _PATHS.get((record.domain, plan.action))
                if path is None:
                    journal.stop("unexpected_service_path")
                    raise CanaryError("unexpected_service_path")
                connection = _connection()
                post_started = time.monotonic()
                try:
                    # Connect failures are known-before-send. Anything after the
                    # first write attempt is delivery_unknown; there is no retry.
                    connection.connect()
                    self._validate(plan, self._config_loader(), journal)
                    if time.monotonic() - observed > MAX_METADATA_AGE:
                        raise CanaryError("stale_inventory")
                    receipt = replace(receipt, status="delivery_unknown", service_calls=1)
                    connection.request("POST", path, body=json.dumps({"entity_id": record.entity_id}),
                                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                                "Connection": "close"})
                    response = connection.getresponse()
                    # HTTP status is transport evidence only. Never use response states.
                    response.read(65_537)
                    receipt = replace(receipt, status="accepted_unverified" if response.status == 200 else "failed")
                except (OSError, http.client.HTTPException):
                    if receipt.service_calls == 0:
                        receipt = replace(receipt, status="failed", reason="not_sent_network_error")
                finally:
                    connection.close()
                    receipt = replace(receipt, post_s=time.monotonic() - post_started)
            if receipt.service_calls or no_op:
                verify_started = time.monotonic()
                proof = self._verifier.finish(plan, config, before)
                receipt = replace(receipt, after=proof.after.states, stable_after=proof.stable_after.states,
                                  verification_s=time.monotonic() - verify_started)
                if proof.critical:
                    journal.stop(proof.critical)
                    receipt = replace(receipt, status="failed", reason=proof.critical)
                elif proof.verified and receipt.status != "failed":
                    receipt = replace(receipt, status="verified", verification_evidence=proof.evidence,
                                      verified_at=proof.verified_at)
        except (CanaryError, reader.AdapterError, OSError, ValueError, KeyError, sqlite3.Error):
            # Do not log exceptions: network errors may embed private URLs/credentials.
            if receipt.service_calls == 0:
                receipt = replace(receipt, reason="revalidation_or_authority_failed")
            elif receipt.status == "verified":
                receipt = replace(receipt, status="accepted_unverified", verified_at=None, verification_evidence=())
        finally:
            receipt = replace(receipt, total_s=time.monotonic() - started)
            if journal is not None:
                try:
                    if reserved:
                        if plan.rollback_of and receipt.status != "verified":
                            journal.stop("rollback_failure")
                        journal.finish(receipt)
                finally:
                    journal.close()
        return receipt
