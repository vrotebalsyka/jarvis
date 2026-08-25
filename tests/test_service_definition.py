#!/usr/bin/env python3
"""Static security contract for the isolated systemd runtime."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
INSTALLER = (PROJECT_DIR / "scripts" / "install-home-butler-service.sh").read_text()
HERMES_CONFIG = (PROJECT_DIR / "hermes" / "config.yaml").read_text()
GATEWAY = (PROJECT_DIR / "config" / "systemd" / "home-butler.service").read_text()
HEARTBEAT = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-heartbeat.service"
).read_text()
HA_PROOF = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-ha-proof.service"
).read_text()
DIALOGUE_QUALIFICATION = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-dialogue-qualification.service"
).read_text()
TIMER = (PROJECT_DIR / "config" / "systemd" / "home-butler-heartbeat.timer").read_text()
STARTUP_SELF_CHECK_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-startup-self-check.timer"
).read_text()
STARTUP_HA = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-startup-ha-check.service"
).read_text()
STARTUP_HA_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-startup-ha-check.timer"
).read_text()
STARTUP_VOICE_STATUS = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-startup-voice-status.service"
).read_text()
STARTUP_VOICE_STATUS_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-startup-voice-status.timer"
).read_text()
INCIDENT_MONITOR = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-incident-monitor.service"
).read_text()
INCIDENT_NOTIFIER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-incident-notifier.service"
).read_text()
INCIDENT_NOTIFIER_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-incident-notifier.timer"
).read_text()
DAILY_REPORT = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-daily-report.service"
).read_text()
DAILY_REPORT_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-daily-report.timer"
).read_text()
OPERATIONS_SUPERVISOR = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-operations-supervisor.service"
).read_text()
OPERATIONS_SUPERVISOR_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-operations-supervisor.timer"
).read_text()
INVENTORY = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-inventory.service"
).read_text()
INVENTORY_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-inventory.timer"
).read_text()
DEVICE_ONBOARDING = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-device-onboarding.service"
).read_text()
DEVICE_ONBOARDING_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-device-onboarding.timer"
).read_text()
RECOVERY = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-recovery.service"
).read_text()
RECOVERY_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-recovery.timer"
).read_text()
AUTOMATION_DIAGNOSTICS = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-automation-diagnostics.service"
).read_text()
AUTOMATION_DIAGNOSTICS_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-automation-diagnostics.timer"
).read_text()
SYSTEM_LOG_DIAGNOSTICS = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-system-log-diagnostics.service"
).read_text()
SYSTEM_LOG_DIAGNOSTICS_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-system-log-diagnostics.timer"
).read_text()
DEVICE_HEALTH = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-device-health.service"
).read_text()
DEVICE_HEALTH_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-device-health.timer"
).read_text()
INTEGRATION_RECOVERY = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-integration-recovery.service"
).read_text()
INTEGRATION_RECOVERY_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-integration-recovery.timer"
).read_text()
AUTOMATION_RECOVERY = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-automation-recovery.service"
).read_text()
AUTOMATION_RECOVERY_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-automation-recovery.timer"
).read_text()
ENTITY_FRESHNESS = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-entity-freshness.service"
).read_text()
ENTITY_FRESHNESS_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-entity-freshness.timer"
).read_text()
CORE_RECOVERY = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-core-recovery.service"
).read_text()
CORE_RECOVERY_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-core-recovery.timer"
).read_text()
VOICE_INTENT = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-voice-intent.service"
).read_text()
ALICE_SKILL = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-alice-skill.service"
).read_text()
LOCAL_CHAT = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-local-chat.service"
).read_text()
ALICE_TUNNEL = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-alice-tunnel.service"
).read_text()
ALICE_HEALTH = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-alice-health.service"
).read_text()
ALICE_HEALTH_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-alice-health.timer"
).read_text()
ALICE_TAILSCALE = (
    PROJECT_DIR / "scripts" / "alice_tailscale_funnel.py"
).read_text()
ALICE_ROTATION_FINALIZE = (
    PROJECT_DIR
    / "config"
    / "systemd"
    / "home-butler-alice-rotation-finalize.service"
).read_text()
ALICE_ROTATION_FINALIZE_PATH = (
    PROJECT_DIR
    / "config"
    / "systemd"
    / "home-butler-alice-rotation-finalize.path"
).read_text()
OUT_OF_BAND_RECOVERY = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-out-of-band-recovery.service"
).read_text()
OUT_OF_BAND_RECOVERY_TIMER = (
    PROJECT_DIR / "config" / "systemd" / "home-butler-out-of-band-recovery.timer"
).read_text()
HOST_RECOVERY_BOOTSTRAP = (
    PROJECT_DIR / "scripts" / "bootstrap-ha-recovery-host.sh"
).read_text()
HOST_RECOVERY_COMMAND = (
    PROJECT_DIR / "scripts" / "ha-recovery-host-command.sh"
).read_text()


class ServiceDefinitionTests(unittest.TestCase):
    def test_services_are_unprivileged_and_use_isolated_runtime(self) -> None:
        for unit in (
            GATEWAY, HEARTBEAT, HA_PROOF, STARTUP_HA, INCIDENT_MONITOR,
            INCIDENT_NOTIFIER, INVENTORY, RECOVERY,
            AUTOMATION_DIAGNOSTICS, SYSTEM_LOG_DIAGNOSTICS, DEVICE_HEALTH,
            AUTOMATION_RECOVERY, INTEGRATION_RECOVERY, ENTITY_FRESHNESS,
            DAILY_REPORT, OPERATIONS_SUPERVISOR,
            STARTUP_VOICE_STATUS,
            CORE_RECOVERY,
            VOICE_INTENT,
            ALICE_SKILL,
        ):
            self.assertIn("User=homebutler", unit)
            self.assertIn("Group=homebutler", unit)
            self.assertIn("NoNewPrivileges=yes", unit)
            self.assertIn("CapabilityBoundingSet=", unit)
            self.assertIn("WorkingDirectory=/opt/home-butler", unit)
            self.assertIn("LoadCredential=home-assistant.token:", unit)
            self.assertNotIn("IPAddressAllow=172.27.192.1/32", unit)
        self.assertIn("User=homebutler", OUT_OF_BAND_RECOVERY)
        self.assertIn("Group=homebutler", OUT_OF_BAND_RECOVERY)
        self.assertIn("NoNewPrivileges=yes", OUT_OF_BAND_RECOVERY)
        self.assertIn("CapabilityBoundingSet=", OUT_OF_BAND_RECOVERY)
        self.assertIn("WorkingDirectory=/opt/home-butler", OUT_OF_BAND_RECOVERY)
        self.assertNotIn("home-assistant.token", OUT_OF_BAND_RECOVERY)
        self.assertIn("LoadCredential=ha-recovery.key:", OUT_OF_BAND_RECOVERY)
        self.assertIn("IPAddressAllow=192.168.1.127/32", OUT_OF_BAND_RECOVERY)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", OUT_OF_BAND_RECOVERY)
        for unit in (GATEWAY, HEARTBEAT, HA_PROOF, STARTUP_HA):
            self.assertIn("IPAddressAllow=172.16.0.0/12", unit)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", INCIDENT_MONITOR)
        self.assertIn("IPAddressAllow=192.168.1.127/32", INCIDENT_MONITOR)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", INCIDENT_NOTIFIER)
        self.assertIn("IPAddressAllow=192.168.1.127/32", INCIDENT_NOTIFIER)
        self.assertIn("IPAddressAllow=127.0.0.0/8", DAILY_REPORT)
        self.assertIn("IPAddressAllow=172.16.0.0/12", DAILY_REPORT)
        self.assertIn("IPAddressAllow=192.168.1.127/32", DAILY_REPORT)
        self.assertIn("IPAddressAllow=127.0.0.0/8", OPERATIONS_SUPERVISOR)
        self.assertIn("IPAddressAllow=172.16.0.0/12", OPERATIONS_SUPERVISOR)
        self.assertIn("IPAddressAllow=192.168.1.127/32", OPERATIONS_SUPERVISOR)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", INVENTORY)
        self.assertIn("IPAddressAllow=192.168.1.127/32", INVENTORY)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", RECOVERY)
        self.assertIn("IPAddressAllow=192.168.1.127/32", RECOVERY)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", AUTOMATION_DIAGNOSTICS)
        self.assertIn("IPAddressAllow=192.168.1.127/32", AUTOMATION_DIAGNOSTICS)
        self.assertIn("IPAddressAllow=172.16.0.0/12", SYSTEM_LOG_DIAGNOSTICS)
        self.assertIn("IPAddressAllow=192.168.1.127/32", SYSTEM_LOG_DIAGNOSTICS)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", DEVICE_HEALTH)
        self.assertIn("IPAddressAllow=192.168.1.127/32", DEVICE_HEALTH)
        self.assertNotIn("/bin/bash", AUTOMATION_RECOVERY)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", AUTOMATION_RECOVERY)
        self.assertNotIn("/bin/bash", INTEGRATION_RECOVERY)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", INTEGRATION_RECOVERY)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", ENTITY_FRESHNESS)
        self.assertIn("IPAddressAllow=192.168.1.127/32", ENTITY_FRESHNESS)
        self.assertNotIn("IPAddressAllow=172.16.0.0/12", CORE_RECOVERY)
        self.assertIn("IPAddressAllow=192.168.1.127/32", CORE_RECOVERY)
        self.assertIn("IPAddressAllow=172.16.0.0/12", VOICE_INTENT)
        self.assertIn("IPAddressAllow=192.168.1.127/32", VOICE_INTENT)
        self.assertIn("IPAddressAllow=127.0.0.0/8", ALICE_SKILL)
        self.assertIn("IPAddressAllow=172.16.0.0/12", ALICE_SKILL)
        self.assertIn("IPAddressAllow=192.168.1.127/32", ALICE_SKILL)
        self.assertIn("BindReadOnlyPaths=/opt/home-butler/hermes/config.yaml", GATEWAY)
        self.assertIn("ExecStart=/opt/home-butler/scripts/model_ha_proof.py --require-gpu", HA_PROOF)
        self.assertNotIn("[Install]", HA_PROOF)
        self.assertIn("install_unit home-butler-ha-proof.service", INSTALLER)
        self.assertIn("ExecStart=/opt/home-butler/scripts/model_ha_proof.py", STARTUP_HA)
        self.assertNotIn("--require-gpu", STARTUP_HA)
        self.assertNotIn("[Install]", STARTUP_HA)
        self.assertIn("install_unit home-butler-startup-ha-check.service", INSTALLER)
        self.assertIn("install_unit home-butler-startup-ha-check.timer", INSTALLER)
        self.assertIn("startup_voice_status.py", INSTALLER)
        self.assertIn(
            "install_unit home-butler-startup-voice-status.service", INSTALLER
        )
        self.assertIn(
            "install_unit home-butler-startup-voice-status.timer", INSTALLER
        )
        self.assertIn("install_unit home-butler-incident-monitor.service", INSTALLER)
        self.assertIn("incident_monitor.py", INSTALLER)
        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/home-butler/scripts/incident_monitor.py",
            INCIDENT_MONITOR,
        )
        self.assertIn(
            "ReadWritePaths=/home/homebutler/.local/state/home-butler/incidents",
            INCIDENT_MONITOR,
        )
        self.assertIn("RestartSec=5", INCIDENT_MONITOR)
        self.assertIn("install_unit home-butler-incident-notifier.service", INSTALLER)
        self.assertIn("install_unit home-butler-incident-notifier.timer", INSTALLER)
        self.assertIn("HOME_BUTLER_ALICE_NOTIFY=live", INCIDENT_NOTIFIER)
        self.assertIn("home_assistant_notify.py", INSTALLER)
        self.assertIn("daily_voice_report.py", INSTALLER)
        self.assertIn("persistent_scheduler.py", INSTALLER)
        self.assertIn("scheduler_natural.py", INSTALLER)
        self.assertIn("install_unit home-butler-daily-report.service", INSTALLER)
        self.assertIn("install_unit home-butler-daily-report.timer", INSTALLER)
        self.assertIn("persistent_scheduler.py --tick --live", DAILY_REPORT)
        self.assertIn("HOME_BUTLER_SCHEDULER_DB=", DAILY_REPORT)
        self.assertNotIn("HOME_BUTLER_DAILY_REPORT_STATUS=", DAILY_REPORT)
        self.assertIn(
            "ReadWritePaths=/home/homebutler/.local/state/home-butler/scheduler",
            DAILY_REPORT,
        )
        self.assertIn(
            "/home/homebutler/.local/state/home-butler/scheduler", ALICE_SKILL
        )
        self.assertIn(
            "/home/homebutler/.local/state/home-butler/scheduler", LOCAL_CHAT
        )
        self.assertIn(
            'ensure_service_directory "$SERVICE_HOME/.local/state/home-butler/scheduler"',
            INSTALLER,
        )
        self.assertNotIn("Restart=", DAILY_REPORT)
        self.assertNotIn("RestartSec=", DAILY_REPORT)
        self.assertIn("StartLimitIntervalSec=0", DAILY_REPORT)
        self.assertIn("operations_supervisor.py", INSTALLER)
        self.assertIn("home_stress_test.py", INSTALLER)
        self.assertIn("install_unit home-butler-operations-supervisor.service", INSTALLER)
        self.assertIn("install_unit home-butler-operations-supervisor.timer", INSTALLER)
        self.assertIn("operations_supervisor.py", OPERATIONS_SUPERVISOR)
        self.assertIn("OnUnitActiveSec=30s", OPERATIONS_SUPERVISOR_TIMER)
        self.assertIn("Persistent=false", OPERATIONS_SUPERVISOR_TIMER)
        self.assertIn("install_unit home-butler-inventory.service", INSTALLER)
        self.assertIn("install_unit home-butler-inventory.timer", INSTALLER)
        self.assertIn("home_assistant_inventory.py", INSTALLER)
        self.assertIn("safe_attribute_sanitizer.py", INSTALLER)
        self.assertIn("install_unit home-butler-recovery.service", INSTALLER)
        self.assertIn("install_unit home-butler-recovery.timer", INSTALLER)
        self.assertIn("HOME_BUTLER_RECOVERY_MODE=live", RECOVERY)
        self.assertIn("HOME_BUTLER_XIAOMI_RECOVERY=disabled", RECOVERY)
        self.assertIn("home_assistant_recovery.py", INSTALLER)
        self.assertIn("install_unit home-butler-automation-diagnostics.service", INSTALLER)
        self.assertIn("install_unit home-butler-automation-diagnostics.timer", INSTALLER)
        self.assertIn("install_unit home-butler-system-log-diagnostics.service", INSTALLER)
        self.assertIn("install_unit home-butler-system-log-diagnostics.timer", INSTALLER)
        self.assertIn("install_unit home-butler-device-health.service", INSTALLER)
        self.assertIn("install_unit home-butler-device-health.timer", INSTALLER)
        self.assertIn("install_unit home-butler-integration-recovery.service", INSTALLER)
        self.assertIn("install_unit home-butler-integration-recovery.timer", INSTALLER)
        self.assertIn("install_unit home-butler-automation-recovery.service", INSTALLER)
        self.assertIn("install_unit home-butler-automation-recovery.timer", INSTALLER)
        self.assertIn("automation_diagnostics.py", INSTALLER)
        self.assertIn("system_log_diagnostics.py", INSTALLER)
        self.assertIn("device_health.py", INSTALLER)
        self.assertIn("integration_recovery.py", INSTALLER)
        self.assertIn("TimeoutStartSec=300", SYSTEM_LOG_DIAGNOSTICS)
        self.assertIn("IPAddressAllow=172.16.0.0/12", SYSTEM_LOG_DIAGNOSTICS)
        self.assertIn("IPAddressAllow=192.168.1.127/32", SYSTEM_LOG_DIAGNOSTICS)
        self.assertIn("OnUnitActiveSec=60s", SYSTEM_LOG_DIAGNOSTICS_TIMER)
        self.assertIn("OnUnitActiveSec=10s", DEVICE_HEALTH_TIMER)
        self.assertIn("RandomizedDelaySec=0", DEVICE_HEALTH_TIMER)
        self.assertIn("OnUnitActiveSec=70s", INTEGRATION_RECOVERY_TIMER)
        self.assertIn(
            "HOME_BUTLER_INTEGRATION_RECOVERY_MODE=live", INTEGRATION_RECOVERY
        )
        self.assertIn("recovery_planner.py", INSTALLER)
        self.assertIn("recovery_playbook_registry.py", INSTALLER)
        self.assertIn("recovery_playbook_executor.py", INSTALLER)
        self.assertIn("device_onboarding.py", INSTALLER)
        self.assertIn("install_unit home-butler-device-onboarding.service", INSTALLER)
        self.assertIn("install_unit home-butler-device-onboarding.timer", INSTALLER)
        self.assertIn("home-butler-device-onboarding.timer", INSTALLER)
        self.assertIn("User=homebutler", DEVICE_ONBOARDING)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", DEVICE_ONBOARDING)
        self.assertIn("IPAddressDeny=any", DEVICE_ONBOARDING)
        self.assertIn("OnUnitActiveSec=1min", DEVICE_ONBOARDING_TIMER)
        self.assertIn("automation_recovery.py", INSTALLER)
        self.assertIn("HOME_BUTLER_AUTOMATION_RECOVERY_MODE=live", AUTOMATION_RECOVERY)
        self.assertIn("HOME_BUTLER_INSTALL_ACTION_TIMERS_MODE", INSTALLER)
        self.assertIn('ACTION_TIMERS_MODE" == "staged"', INSTALLER)
        self.assertIn(
            "disable --now \\\n    home-butler-recovery.timer home-butler-core-recovery.timer",
            INSTALLER,
        )
        self.assertIn("install_unit home-butler-entity-freshness.service", INSTALLER)
        self.assertIn("install_unit home-butler-entity-freshness.timer", INSTALLER)
        self.assertIn("entity_freshness.py", INSTALLER)
        self.assertIn("OnUnitActiveSec=5min", ENTITY_FRESHNESS_TIMER)
        self.assertIn("install_unit home-butler-core-recovery.service", INSTALLER)
        self.assertIn("install_unit home-butler-core-recovery.timer", INSTALLER)
        self.assertIn("HOME_BUTLER_CORE_RECOVERY_MODE=live", CORE_RECOVERY)
        self.assertIn("home_assistant_core_recovery.py", INSTALLER)
        self.assertIn("incident_status.py", INSTALLER)
        self.assertIn("install_unit home-butler-voice-intent.service", INSTALLER)
        self.assertIn("alice_voice_bridge.py", INSTALLER)
        self.assertIn("HOME_BUTLER_ALICE_VOICE=live", VOICE_INTENT)
        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/home-butler/scripts/alice_voice_bridge.py",
            VOICE_INTENT,
        )
        self.assertIn(
            "ReadWritePaths=/home/homebutler/.local/state/home-butler/incidents",
            VOICE_INTENT,
        )
        self.assertIn("RestartSec=5", VOICE_INTENT)
        self.assertIn("install_unit home-butler-alice-skill.service", INSTALLER)
        self.assertIn("install_unit home-butler-alice-tunnel.service", INSTALLER)
        self.assertIn("install_unit home-butler-alice-health.service", INSTALLER)
        self.assertIn("install_unit home-butler-alice-health.timer", INSTALLER)
        self.assertIn("install_unit home-butler-alice-finalize.service", INSTALLER)
        self.assertIn("install_unit home-butler-alice-finalize.path", INSTALLER)
        self.assertIn(
            "install_unit home-butler-alice-rotation-finalize.service", INSTALLER
        )
        self.assertIn(
            "install_unit home-butler-alice-rotation-finalize.path", INSTALLER
        )
        self.assertIn("alice_claim_finalizer.py", INSTALLER)
        self.assertIn("alice_skill_gateway.py", INSTALLER)
        self.assertIn("alice_skill_health.py", INSTALLER)
        self.assertIn("rotate-alice-webhook.py", INSTALLER)
        self.assertIn("owner_chat.py", INSTALLER)
        self.assertIn("model_runtime_policy.py", INSTALLER)
        self.assertIn("memory_store.py", INSTALLER)
        self.assertIn("behavior_preferences.py", INSTALLER)
        self.assertIn("safe_maintenance.py", INSTALLER)
        self.assertIn("maintenance_worker.py", INSTALLER)
        self.assertIn("context_builder.py", INSTALLER)
        self.assertIn("turn_observability.py", INSTALLER)
        self.assertIn("capability_catalog.py", INSTALLER)
        self.assertIn("bounded_ha_agent.py", INSTALLER)
        self.assertIn(
            'ensure_service_directory "$SERVICE_HOME/.local/state/home-butler/memory"',
            INSTALLER,
        )
        self.assertIn("managed_runtime_scripts=()", INSTALLER)
        self.assertIn("declare -A managed_runtime_script_names=()", INSTALLER)
        self.assertIn("Refusing unmanaged runtime script:", INSTALLER)
        self.assertIn("update-home-butler-lan-forward.sh", INSTALLER)
        self.assertIn(
            'find "$RUNTIME_DIR/scripts" -maxdepth 1 -type f -print0',
            INSTALLER,
        )
        self.assertIn("ha_entity_query.py", INSTALLER)
        self.assertIn("LoadCredential=alice-skill-secret:", ALICE_SKILL)
        self.assertIn("LoadCredential=alice-skill-secret-next:", ALICE_SKILL)
        self.assertIn("LoadCredential=alice-skill-id:", ALICE_SKILL)
        self.assertIn("LoadCredential=alice-owner-ids:", ALICE_SKILL)
        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/home-butler/scripts/alice_skill_gateway.py",
            ALICE_SKILL,
        )
        for unit in (ALICE_SKILL, LOCAL_CHAT):
            self.assertIn(
                "HOME_BUTLER_MEMORY_DB=/home/homebutler/.local/state/home-butler/memory/memory.db",
                unit,
            )
            self.assertIn(
                "/home/homebutler/.local/state/home-butler/memory",
                unit,
            )
        enable_block = INSTALLER.split("systemctl enable --now", 1)[1].split(
            "systemctl is-enabled", 1
        )[0]
        self.assertNotIn("home-butler-voice-intent", enable_block)
        self.assertIn(
            "disable --now home-butler-voice-intent.service", INSTALLER
        )
        self.assertNotIn("home-butler-alice-skill", enable_block)
        self.assertNotIn("home-butler-alice-tunnel", enable_block)
        self.assertIn("User=root", ALICE_HEALTH)
        self.assertIn("NoNewPrivileges=yes", ALICE_HEALTH)
        self.assertIn("CapabilityBoundingSet=", ALICE_HEALTH)
        self.assertNotIn("home-assistant.token", ALICE_HEALTH)
        self.assertIn("LoadCredential=alice-skill-secret:", ALICE_HEALTH)
        self.assertIn("alice_skill_health.py --recover", ALICE_HEALTH)
        self.assertIn("RuntimeDirectoryMode=0700", ALICE_HEALTH)
        self.assertIn("RuntimeDirectoryPreserve=yes", ALICE_HEALTH)
        self.assertIn("ProtectSystem=strict", ALICE_HEALTH)
        self.assertIn("OnBootSec=30s", ALICE_HEALTH_TIMER)
        self.assertIn("OnUnitActiveSec=10s", ALICE_HEALTH_TIMER)
        self.assertIn("RandomizedDelaySec=0", ALICE_HEALTH_TIMER)
        self.assertIn("User=homebutler", STARTUP_VOICE_STATUS)
        self.assertIn("Group=homebutler", STARTUP_VOICE_STATUS)
        self.assertIn("NoNewPrivileges=yes", STARTUP_VOICE_STATUS)
        self.assertIn("CapabilityBoundingSet=", STARTUP_VOICE_STATUS)
        self.assertIn("ProtectSystem=strict", STARTUP_VOICE_STATUS)
        self.assertIn("ProtectHome=read-only", STARTUP_VOICE_STATUS)
        self.assertIn("RuntimeDirectoryMode=0700", STARTUP_VOICE_STATUS)
        self.assertIn("RuntimeDirectoryPreserve=yes", STARTUP_VOICE_STATUS)
        self.assertIn("LoadCredential=home-assistant.token:", STARTUP_VOICE_STATUS)
        self.assertIn("LoadCredential=alice-skill-secret:", STARTUP_VOICE_STATUS)
        self.assertIn("LoadCredential=alice-skill-secret-next:", STARTUP_VOICE_STATUS)
        self.assertIn("LoadCredential=alice-skill-id:", STARTUP_VOICE_STATUS)
        self.assertIn("LoadCredential=alice-owner-ids:", STARTUP_VOICE_STATUS)
        self.assertIn("startup_voice_status.py", STARTUP_VOICE_STATUS)
        self.assertIn(
            'Path("/run/credentials/home-butler-startup-voice-status.service")',
            (PROJECT_DIR / "scripts" / "home_assistant_read.py").read_text(),
        )
        self.assertIn("OnBootSec=180s", STARTUP_VOICE_STATUS_TIMER)
        self.assertIn("OnActiveSec=180s", STARTUP_VOICE_STATUS_TIMER)
        self.assertIn("OnUnitActiveSec=1min", STARTUP_VOICE_STATUS_TIMER)
        self.assertIn("Persistent=false", STARTUP_VOICE_STATUS_TIMER)
        self.assertIn("RandomizedDelaySec=0", STARTUP_VOICE_STATUS_TIMER)
        self.assertIn("User=root", ALICE_TUNNEL)
        self.assertIn("NoNewPrivileges=yes", ALICE_TUNNEL)
        self.assertIn("CapabilityBoundingSet=", ALICE_TUNNEL)
        self.assertIn("Requires=home-butler-alice-skill.service", ALICE_TUNNEL)
        self.assertIn("tailscaled.service", ALICE_TUNNEL)
        self.assertIn('FUNNEL_TARGET = "http://127.0.0.1:8765"', ALICE_TAILSCALE)
        self.assertIn("alice_tailscale_funnel.py --ensure", ALICE_TUNNEL)
        self.assertIn("StartLimitIntervalSec=0", ALICE_TUNNEL)
        self.assertIn("RestartSec=30", ALICE_TUNNEL)
        self.assertIn("TasksMax=32", ALICE_TUNNEL)
        self.assertNotIn("ngrok", ALICE_TUNNEL)
        self.assertNotIn("authtoken", ALICE_TUNNEL)
        self.assertIn(
            "PathExists=/home/homebutler/.local/state/home-butler/alice/webhook-next-used",
            ALICE_ROTATION_FINALIZE_PATH,
        )
        self.assertIn("ExecStartPre=/usr/bin/sleep 5", ALICE_ROTATION_FINALIZE)
        self.assertIn(
            "rotate-alice-webhook.py --commit", ALICE_ROTATION_FINALIZE
        )
        self.assertIn("ProtectSystem=strict", ALICE_ROTATION_FINALIZE)
        self.assertIn("ProtectHome=read-only", ALICE_ROTATION_FINALIZE)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", ALICE_ROTATION_FINALIZE)
        self.assertIn("IPAddressDeny=any", ALICE_ROTATION_FINALIZE)
        self.assertNotIn("alice-skill-secret", ALICE_ROTATION_FINALIZE)
        self.assertIn("install_unit home-butler-out-of-band-recovery.service", INSTALLER)
        self.assertIn("install_unit home-butler-out-of-band-recovery.timer", INSTALLER)
        self.assertIn("out_of_band_recovery.py", INSTALLER)
        self.assertIn("ha-recovery-known_hosts", INSTALLER)
        self.assertIn("TimeoutStartSec=720", OUT_OF_BAND_RECOVERY)
        self.assertIn("disable --now home-butler-out-of-band-recovery.timer", INSTALLER)
        self.assertNotIn("home-butler-out-of-band-recovery", enable_block)

    def test_installer_does_not_grant_access_to_root_tree(self) -> None:
        self.assertNotIn("setfacl", INSTALLER)
        self.assertNotIn("chmod o+x /root", INSTALLER)
        self.assertIn('readonly RUNTIME_DIR="/opt/home-butler"', INSTALLER)
        self.assertIn("SERVICE_UID > 0", INSTALLER)
        self.assertIn("! -L", INSTALLER)
        self.assertIn("Refusing unsafe unit target", INSTALLER)
        for policy in ("AGENTS.md", "HEARTBEAT.md", "TOOLS.md"):
            self.assertIn(policy, INSTALLER)
        verifier = (PROJECT_DIR / "scripts" / "verify-runtime-policy.py").read_text()
        self.assertIn("build_context_files_prompt", verifier)
        self.assertIn("RUNTIME_POLICY_OK", verifier)
        self.assertIn("context_file_max_chars: 25000", HERMES_CONFIG)
        self.assertIn('cd "$RUNTIME_DIR"', INSTALLER)
        self.assertNotIn(
            'local name="$1" source="$UNIT_SOURCE_DIR/$name"',
            INSTALLER,
        )

    def test_restart_and_heartbeat_cadence_are_bounded(self) -> None:
        self.assertIn("Restart=on-failure", GATEWAY)
        self.assertIn("RestartSec=10", GATEWAY)
        self.assertIn("StartLimitIntervalSec=300", GATEWAY)
        self.assertIn("StartLimitBurst=5", GATEWAY)
        self.assertIn("OnUnitActiveSec=10min", TIMER)
        self.assertIn("OnBootSec=1min", TIMER)
        self.assertIn("RandomizedDelaySec=30", TIMER)
        self.assertIn("OnBootSec=90s", STARTUP_SELF_CHECK_TIMER)
        self.assertIn("RandomizedDelaySec=10s", STARTUP_SELF_CHECK_TIMER)
        self.assertIn("Persistent=true", STARTUP_SELF_CHECK_TIMER)
        self.assertNotIn("OnUnitActiveSec=", STARTUP_SELF_CHECK_TIMER)
        self.assertIn("OnBootSec=90s", STARTUP_HA_TIMER)
        self.assertIn("RandomizedDelaySec=15", STARTUP_HA_TIMER)
        self.assertIn("Persistent=true", STARTUP_HA_TIMER)
        self.assertIn("OnUnitActiveSec=10s", INCIDENT_NOTIFIER_TIMER)
        self.assertIn("RandomizedDelaySec=2", INCIDENT_NOTIFIER_TIMER)
        self.assertIn("Persistent=true", INCIDENT_NOTIFIER_TIMER)
        self.assertIn("OnBootSec=30s", DAILY_REPORT_TIMER)
        self.assertIn("OnUnitActiveSec=15s", DAILY_REPORT_TIMER)
        self.assertNotIn("OnCalendar=", DAILY_REPORT_TIMER)
        self.assertIn("Persistent=true", DAILY_REPORT_TIMER)
        self.assertIn("AccuracySec=1s", DAILY_REPORT_TIMER)
        self.assertIn("RandomizedDelaySec=0", DAILY_REPORT_TIMER)
        self.assertIn("OnUnitActiveSec=30s", INVENTORY_TIMER)
        self.assertIn("RandomizedDelaySec=15", INVENTORY_TIMER)
        self.assertIn("Persistent=true", INVENTORY_TIMER)
        self.assertIn("OnUnitActiveSec=1min", RECOVERY_TIMER)
        self.assertIn("RandomizedDelaySec=5", RECOVERY_TIMER)
        self.assertIn("Persistent=true", RECOVERY_TIMER)
        self.assertIn("OnUnitActiveSec=1min", CORE_RECOVERY_TIMER)
        self.assertIn("RandomizedDelaySec=5", CORE_RECOVERY_TIMER)
        self.assertIn("Persistent=true", CORE_RECOVERY_TIMER)
        self.assertIn("OnBootSec=5min", OUT_OF_BAND_RECOVERY_TIMER)
        self.assertIn("OnUnitActiveSec=1min", OUT_OF_BAND_RECOVERY_TIMER)
        self.assertIn("RandomizedDelaySec=5", OUT_OF_BAND_RECOVERY_TIMER)

    def test_every_managed_source_unit_has_the_installer_marker(self) -> None:
        marker = (
            "# Managed by /root/Jarvis/home-butler/scripts/"
            "install-home-butler-service.sh"
        )
        unit_dir = PROJECT_DIR / "config" / "systemd"
        missing = [
            path.name
            for path in sorted(unit_dir.glob("home-butler*"))
            if path.is_file() and path.read_text().splitlines()[0] != marker
        ]
        self.assertEqual(missing, [])

    def test_dialogue_qualification_allows_six_slow_model_turns(self) -> None:
        self.assertIn("TimeoutStartSec=600", DIALOGUE_QUALIFICATION)
        self.assertIn("Restart=no", DIALOGUE_QUALIFICATION)
        self.assertNotIn("Restart=on-failure", DIALOGUE_QUALIFICATION)

    def test_host_recovery_bootstrap_exposes_only_one_forced_command(self) -> None:
        self.assertNotIn('""":"', HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("[--repair]", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("usermod --password '*'", HOST_RECOVERY_BOOTSTRAP)
        self.assertNotIn("*NP*", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("shadow_password", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("authenticationmethods publickey", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn(
            'install -o root -g root -m 0644 -- "$temporary" "$AUTHORIZED_KEYS_FILE"',
            HOST_RECOVERY_BOOTSTRAP,
        )
        self.assertIn("== '0:644'", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn('restrict,from="192.168.1.0/24",command=', HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("Match User homebutler-recovery", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("AuthenticationMethods publickey", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("PasswordAuthentication no", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("KbdInteractiveAuthentication no", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("DisableForwarding yes", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("PermitTTY no", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("NOSETENV: /usr/local/sbin/homebutler-recover-root \"\"", HOST_RECOVERY_BOOTSTRAP)
        self.assertNotIn("/var/run/docker.sock", HOST_RECOVERY_BOOTSTRAP)
        self.assertNotIn("* ALL", HOST_RECOVERY_BOOTSTRAP)
        self.assertIn("--filter label=io.hass.type=core", HOST_RECOVERY_COMMAND)
        self.assertIn("docker container restart --timeout 240", HOST_RECOVERY_COMMAND)
        self.assertIn("/run/homebutler-ha-maintenance.lock", HOST_RECOVERY_COMMAND)

    def test_boot_orders_fallback_and_waits_for_primary_gpu(self) -> None:
        self.assertIn("After=network-online.target ollama.service", GATEWAY)
        self.assertIn("Wants=network-online.target ollama.service", GATEWAY)
        self.assertIn(
            "ollama_endpoint.py --prefer-primary-seconds 45 --wait-seconds 60",
            GATEWAY,
        )
        self.assertIn("ollama.service home-butler.service", HEARTBEAT)
        self.assertIn("ollama.service home-butler.service", STARTUP_HA)


if __name__ == "__main__":
    unittest.main()
