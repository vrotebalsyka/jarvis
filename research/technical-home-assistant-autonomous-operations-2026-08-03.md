---
topic: "Home Assistant autonomous operations agent for Home Butler"
type: "technical"
goals: "Design a safe local operational agent for incident response, HAOS recovery, changing device IPs, switch/button/light control, and critical Alice voice notifications"
date: "2026-08-03"
methodology: "Direct multi-source web research plus three parallel research tracks using official documentation and primary GitHub repositories. Claims are reconciled below with inline citations, accessed date, source tier and confidence."
---

# Research Report — Home Assistant Autonomous Operations Agent

> **Type:** technical | **Date:** 2026-08-03 | **Constraints:** local-first; HAOS at 192.168.1.127:8123; Home Butler on Windows/WSL; no cloud fallback; physical actions must be bounded and verifiable
>
> **Goals:** compare existing projects; define an operational-agent architecture; recover HAOS and LAN devices safely; manage switches, buttons and lights; deliver critical notifications through the existing Alice speakers; assess inbound voice as a later path.
>
> **Assumptions:** “operational personnel” means rapid detection, diagnosis, bounded remediation and verification—not unrestricted shell or router administration. Device IP repair must rely on authoritative discovery/integration data and must not guess addresses.

## Technology Landscape

### Overview of Approaches

No reviewed project is a complete drop-in “Jarvis” that safely detects HA/LAN
incidents, repairs changing device addresses, restarts HAOS and controls the home.
The useful projects are narrower building blocks:

- Home Assistant itself provides the authoritative state/event bus and bounded
  service calls. Its WebSocket API can stream state changes and supports
  authenticated subscriptions, while the REST API provides fixed service
  endpoints. This is the correct event and action plane, not screen scraping
  ([HA WebSocket API](https://developers.home-assistant.io/docs/api/websocket/),
  [HA REST API](https://developers.home-assistant.io/docs/api/rest/); accessed
  2026-08-03; confidence: high).
- NetAlertX provides continuous local-network asset discovery, IP-address drift
  detection and several discovery sources including ARP, DHCP leases and SNMP.
  It can feed Home Assistant or webhooks, so it is a candidate evidence source
  for “same device, new IP”, not a universal recovery engine
  ([NetAlertX](https://github.com/netalertx/NetAlertX); accessed 2026-08-03;
  confidence: high).
- Watchman and Unavailable Entities Sensor detect missing, unknown or
  unavailable Home Assistant entities. They are useful detection patterns but
  do not implement device-specific repair
  ([Watchman](https://github.com/dummylabs/thewatchman),
  [Unavailable Entities Sensor](https://github.com/jazzyisj/unavailable-entities-sensor);
  accessed 2026-08-03; confidence: high).
- Spook adds Home Assistant repair/configuration diagnostics. Its role is
  configuration integrity rather than LAN rediscovery or physical remediation
  ([Spook](https://github.com/frenck/spook); accessed 2026-08-03; confidence:
  medium-high).
- LocalTuya, Tuya Local and Meross LAN show why recovery must be
  integration-specific. Tuya devices may need device identity/local-key data,
  while Meross LAN explicitly handles dynamic IP changes inside the
  integration. A generic agent must never guess an IP or edit HA storage
  directly
  ([LocalTuya](https://github.com/homeassistant-projects/ha-localtuya),
  [Tuya Local](https://github.com/make-all/tuya-local),
  [Meross LAN](https://github.com/krahabb/meross_lan); accessed 2026-08-03;
  confidence: high).

### Performance and Benchmarks

The reviewed primary sources do not publish comparable end-to-end recovery
benchmarks. For this project, acceptance must therefore be measured locally:
event detection latency, time to confirm a fault, time to perform one bounded
recovery action, verification latency and false-positive rate. The current
ten-minute heartbeat is suitable for a periodic audit but not rapid incident
response; Home Assistant’s streaming WebSocket event interface is the supported
mechanism for seconds-level state observation
([HA WebSocket API](https://developers.home-assistant.io/docs/api/websocket/);
accessed 2026-08-03; confidence: high).

### Community Health and Maturity

Home Assistant’s official APIs and lifecycle mechanisms are the stable core.
The reviewed GitHub integrations are active, useful components but remain
separate community projects with their own compatibility and upgrade risk.
Home Butler should borrow their patterns or consume their supported outputs,
not copy their internals into one privileged process
([Home Assistant config entries](https://developers.home-assistant.io/docs/config_entries_index/),
[NetAlertX](https://github.com/netalertx/NetAlertX),
[YandexStation](https://github.com/AlexxIT/YandexStation); accessed 2026-08-03;
confidence: high for the architectural conclusion, medium for future
third-party compatibility).

### Integration and Compatibility

Home Assistant has first-class config-entry lifecycle and reconfiguration
flows. When an integration supports discovery updates, the integration should
update connection information using stable device identity such as a registered
MAC address. Home Butler should invoke only supported reload/reconfigure paths;
direct modification of `.storage/core.config_entries` is excluded
([config entries](https://developers.home-assistant.io/docs/config_entries_index/),
[reconfigure flows](https://developers.home-assistant.io/docs/core/integration/config_flow/),
[discovery update rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/discovery-update-info/);
accessed 2026-08-03; confidence: high).

The live HAOS probe on 2026-08-03 found Home Assistant 2026.5.2, two
`media_player.yandex_station_*` entities, `tts.yandex_station_say`, and the
`yandex_station.send_command` service. The installed YandexStation component
supports sending TTS and text commands, with richer capability for supported
local stations. Therefore outbound critical voice announcements are feasible
now; inbound conversational voice remains a separate later feature
([YandexStation](https://github.com/AlexxIT/YandexStation); accessed 2026-08-03;
confidence: high, corroborated by local authenticated read-only API probe).

### Inbound Alice Voice: What Actually Works

No mature reviewed project provides a single turnkey path from arbitrary
speech on a Yandex Station through a local Ollama agent to bounded HA actions.
The supported pieces have different roles. `Yandex Smart Home` translates
ordinary speech into typed device capabilities and is therefore the preferred
path for routine light/switch commands, but it is not a free-text LLM channel
([Yandex Smart Home capabilities](https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/capability-types),
[dext0r/yandex_smart_home](https://github.com/dext0r/yandex_smart_home);
accessed 2026-08-03; source tier: primary; confidence: high).

`dext0r/ha-yandex-station-intents` creates Yandex scenarios for a predefined
phrase list and emits a canonical `yandex_intent`. In websocket mode the event
also identifies the station and room, but that mode relies on an undocumented
Yandex mechanism; device mode is less expressive. The project documents a
200-scenario limit, so this is a bounded-command transport, not arbitrary STT
([Yandex Station Intents README](https://github.com/dext0r/ha-yandex-station-intents/blob/master/README.md),
[v0.7.0 release](https://github.com/dext0r/ha-yandex-station-intents/releases/tag/v0.7.0);
accessed 2026-08-03; source tier: primary project documentation; confidence:
medium-high).

The existing `AlexxIT/YandexStation` integration can emit `yandex_scenario`
for predefined scenario actions and can speak TTS, but its documentation says
that the original user phrase cannot be recovered from that scenario path.
Its conversation entity sends text from HA to Alice; it is not a microphone
stream into Assist
([YandexStation incoming commands](https://github.com/AlexxIT/YandexStation/blob/master/README.md#получение-команд-от-станции),
[conversation implementation](https://github.com/AlexxIT/YandexStation/blob/master/custom_components/yandex_station/conversation.py);
accessed 2026-08-03; source tier: primary project documentation/source;
confidence: high).

Arbitrary recognized text requires a Yandex Dialogs custom skill. Yandex sends
`original_utterance`/`command` to a public HTTPS backend and requires a full
certificate chain; a general skill must answer within 4.5 seconds and normally
uses an activation name. A private skill can restrict distribution, but a
private flag alone is not sufficient authorization for HA control
([request format](https://yandex.ru/dev/dialogs/alice/doc/ru/request),
[deployment and timeout](https://yandex.ru/dev/dialogs/alice/doc/ru/publish-settings),
[activation](https://yandex.ru/dev/dialogs/alice/doc/ru/activation),
[private access](https://yandex.ru/dev/dialogs/alice/doc/ru/access);
accessed 2026-08-03; source tier: official primary; confidence: high).
`AlexxIT/YandexDialogs` demonstrates the event flow, but the repository has no
discoverable license, so its code was not copied into Home Butler
([YandexDialogs README](https://github.com/AlexxIT/YandexDialogs),
[GitHub repository metadata](https://api.github.com/repos/AlexxIT/YandexDialogs),
[GitHub licensing guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository);
accessed 2026-08-03; source tier: primary; confidence: high).

The safe composition is therefore three layers: current Yandex Smart Home for
routine devices; a local exact-intent bridge for a few operational commands;
and later a minimal private HTTPS/OAuth voice edge for free conversation. The
public edge must never receive the HA long-lived token or expose ports 8123 or
11434. Home Assistant's Conversation API can accept text at the local side and
return structured speech, while HA's own guidance recommends narrowly exposed
entities for voice control
([HA Conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/),
[HA exposed entities](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/),
[HA securing](https://www.home-assistant.io/docs/configuration/securing/);
accessed 2026-08-03; source tier: official primary; confidence: high for the
components, medium-high for the composed architecture).

## Comparative Analysis

### Top Options Head-to-Head

Home Assistant WebSocket/REST is the only option that is simultaneously
authoritative for entity state, already authenticated, local and able to
perform narrow supported actions. NetAlertX is stronger for independent LAN
presence/IP evidence, while Watchman-style sensors are simpler but slower and
cannot remediate. Integration-native discovery/reload is safer than a generic
IP writer because it retains stable device identity and integration ownership.
Therefore the selected design uses HA as the control plane, the existing
Network Scanner as supporting identity evidence, and separate per-integration
recovery adapters. No third-party project receives the HA token or arbitrary
execution rights.

### Decision Matrix

| Option | Detection | Remediation | HA-native | Local-first | Safety boundary | Role in Home Butler |
| --- | --- | --- | --- | --- | --- | --- |
| HA WebSocket + REST | State/event stream | Fixed HA services | Yes | Yes | Strong if paths are allowlisted | Primary event/action plane |
| NetAlertX | LAN presence and IP drift | Workflow/webhook only | Integrates | Yes by default | Needs scan scope limits | Optional identity/IP evidence |
| Watchman / unavailable sensor | Missing/unavailable HA entities | Report only | Yes | Yes | Read-oriented | Detection pattern/input |
| Spook | Config reference/repair issues | HA repair helpers | Yes | Yes | Integration-defined | Config-integrity input |
| LocalTuya / Tuya Local | Tuya discovery/status | Integration-specific reconfigure | Yes | Mostly local | Device keys and DPIDs are sensitive | Tuya recovery adapter only |
| Meross LAN | Meross state/discovery | Dynamic-IP handling/failover | Yes | Local-capable | Integration-owned | Pattern to emulate |
| YandexStation | Speaker status | TTS/commands | Yes | Local + cloud by device | Fixed speaker/TTS allowlist | Critical outbound alerts |

### Migration and Lock-in Risks

Tying recovery to raw entity IDs, guessed IPs or undocumented HA storage would
make upgrades fragile. The incident database should store stable HA device and
config-entry identifiers plus observed entity IDs. Every repair adapter must
declare the integration and HA version it supports; unsupported integrations
stop at diagnosis and notification
([config entries](https://developers.home-assistant.io/docs/config_entries_index/),
[reconfigure flows](https://developers.home-assistant.io/docs/core/integration/config_flow/);
accessed 2026-08-03; confidence: high).

## Implementation Considerations

### Recommended Architecture Patterns

Use a deterministic state machine around the local model:

`observed → confirmed → diagnosed → planned → policy-approved → acted → verified → resolved/escalated`.

The model may classify and explain evidence, but it never receives arbitrary
shell, router-admin or unrestricted `ha_call_service`. Deterministic adapters
perform exact reads/actions, persist an incident journal, enforce debounce,
cooldown and rate limits, and verify every state-changing action. For
`switch`/`button`, the already implemented owner path remains: exact entity ID,
exact model tool call, fixed service path, POST and GET readback. Automatic
recovery uses a separate policy and cannot inherit owner-chat authority.

The least-disruptive recovery ladder is:

1. confirm the entity/device is truly unavailable rather than stale/transient;
2. refresh/update the entity;
3. reload its exact config entry;
4. use an integration-supported rediscovery/reconfigure flow with stable
   identity evidence;
5. check configuration and restart Home Assistant Core;
6. reboot HAOS only through an independent, separately authorized recovery
   channel.

Home Assistant exposes update/reload/check-config/restart actions, and its
restart action validates configuration before restart
([Home Assistant actions](https://www.home-assistant.io/integrations/homeassistant),
[safe restart](https://www.home-assistant.io/actions/homeassistant.restart/);
accessed 2026-08-03; confidence: high).

### Common Pitfalls and Gotchas

- A single `unavailable` event is not proof of a broken device; debounce and a
  second independent probe are mandatory.
- If Home Assistant itself is down, its own REST/WebSocket API cannot restart
  it. The local probe found port 8123 open and SSH port 22 open, but Observer
  port 4357 closed. No out-of-band HAOS restart will be enabled until the exact
  SSH/Supervisor recovery identity and least privilege are audited (local probe
  2026-08-03; confidence: high).
- Restart loops can make a recoverable outage worse. Restart requires a
  configuration check, one-attempt budget, cooldown and post-start health
  verification
  ([safe restart](https://www.home-assistant.io/actions/homeassistant.restart/);
  accessed 2026-08-03; confidence: high).
- Directly replacing Tuya host data from an ARP guess can bind the wrong device
  or corrupt an integration. Stable identity and integration-specific
  reconfiguration are required
  ([LocalTuya](https://github.com/homeassistant-projects/ha-localtuya),
  [Tuya Local](https://github.com/make-all/tuya-local); accessed 2026-08-03;
  confidence: high).
- LocalTuya 5.2.5 registers a device as `local_<device id>` while its config
  entry diagnostics keys devices by the unprefixed ID. Normalize that exact
  integration-owned prefix before hashing; never persist the raw ID
  ([LocalTuya 5.2.5 source](https://github.com/rospogrigio/localtuya/blob/v5.2.5/custom_components/localtuya/common.py#L402-L428);
  accessed 2026-08-03; confidence: high).
- Voice alerts can spam the household. Severity thresholds, quiet hours,
  deduplication and critical-only override are required; message text must be
  rendered from incident facts rather than freely generated.

### Security and Compliance

Keep the current dedicated HA token outside model context and continue
redacting attributes/sensitive strings. Add capabilities as narrow tools:
read-all sanitized state; explicit `switch`/`button`; bounded light control;
specific recovery actions; specific TTS target. LAN discovery is read-only and
restricted to the local subnet/device inventory. Destructive Supervisor/OS
endpoints, arbitrary SSH/shell, locks, alarms and raw service calls remain
denied. The Supervisor API includes highly destructive endpoints, which is why
an unrestricted Supervisor token must not be exposed to the model
([Supervisor endpoints](https://developers.home-assistant.io/docs/api/supervisor/endpoints/);
accessed 2026-08-03; confidence: high).

## Key Findings

1. Build the agent as an orchestrator of narrow deterministic adapters, not a
   generally privileged LLM.
2. Home Assistant WebSocket events should replace the ten-minute timer as the
   rapid detection path; the timer remains the independent periodic audit.
3. Changing IPs are repaired by stable identity plus supported integration
   discovery/reconfigure, never by guessing or editing HA storage.
4. Current `switch` and `button` interaction is a valid completed baseline, but
   automatic incident remediation must use a distinct policy and journal.
5. Safe HA Core restart is possible while the API is alive. Recovery while HA
   is dead still needs an audited out-of-band channel.
6. Critical Alice announcements work now. Fixed inbound commands can use a
   fail-closed `yandex_intent`/`yandex_scenario` bridge; free conversation is a
   separate private HTTPS/OAuth skill and must not expose HA or Ollama.

## Locally Validated Implementation

The architecture above was implemented and validated on 2026-08-03:

- a permanent WebSocket observer records only entity ID, normalized state and
  time in a private SQLite ledger, with a 60-second debounce;
- a private registry/LAN inventory mapped 194 registered entities, 26 config
  entries and 17 already-discovered LAN devices without exposing names,
  attributes, IP/MAC or credentials to the model. Exact LocalTuya 5.2.5
  identifier normalization proved one live identity in stable network state;
- a bounded worker supports `localtuya.reload` for new LocalTuya incidents and
  `homeassistant.reload_config_entry` for one exact Tuya Local entity; both
  have baseline protection, one-action-per-incident, cooldown and GET
  verification. Durable identity observations distinguish a confirmed IP
  change from generic integration unavailability without writing HA storage;
- a Core worker requires a new five-minute system incident, two responding REST
  probes, `homeassistant.check_config`, one restart per six hours and API-return
  verification. The live Core exposes 230 components but neither `hassio` nor
  `supervisor`; its `supervisor/api` WebSocket command is unknown and Supervisor
  REST routes return 404. A fully dead Core therefore remains out-of-band
  rather than falsely “fixed”;
- Alice TTS was accepted by the live `tts.yandex_station_say` service using a
  fixed critical message and fixed station allowlist;
- a permanent incoming Alice bridge now subscribes to `yandex_intent` and
  `yandex_scenario`, accepts four exact routes from two exact stations, invokes
  model proof/control boundaries, deduplicates events, replies to the same
  station and journals no raw speech. It is active and waiting; no scenario was
  fired and its ledger remains at zero actions;
- the owner tool now covers exact `switch`, `button` and `light` actions. Live
  Ollama dry-runs emitted exact light, switch and button tool calls with zero HA
  service calls; the latest switch/button proofs used the fully offloaded Vulkan
  endpoint;
- 177 automated tests ran: 176 passed and one intentionally opt-in live test
  was skipped.
- an independent Ubuntu-host recovery worker, pinned SSH identity and forced
  container wrapper were implemented. The systemd unit ran live with zero
  candidates and zero SSH/restart calls; its timer remains deliberately
  disabled until the target-host bootstrap and healthy end-to-end proof.

These are local acceptance results rather than portable product benchmarks.

## Strategic Recommendations

Keep the deployed ascending-risk ladder: notification → integration-owned IP
discovery/reload → bounded Core restart. Do not add external direct IP writes
or HAOS reboot. For
voice, enable the four fixed intents first; build free conversation only behind
a private OAuth-protected edge. Finish the already prepared out-of-band channel
only by bootstrapping its forced-command identity on the Ubuntu target and
proving a healthy zero-restart invocation before enabling the timer.

## Out-of-Band Container Recovery Research and Decision

The live endpoint is not an HAOS/Supervisor target. Its SSH banner identifies
Ubuntu OpenSSH 9.6p1, the HA frontend answers on 8123, Observer 4357 is closed,
and live HA API probes expose neither `hassio` nor Supervisor. The applicable
recovery boundary is therefore the Ubuntu Docker host, not an HAOS reboot API
(local network/API probes, 2026-08-03; confidence: high).

The official Home Assistant Container path uses the official container image
and host networking. The Core image declares `io.hass.type=core` and a
240-second stop signal timeout; Docker accepts an explicit stop timeout for a
restart. The implementation consequently refuses ambiguous identity: exactly
one full 64-character container ID must match the Core label, an official HA
image name and a `/config` mount, and every operation uses that exact ID. It
uses `docker container restart --timeout 240`, not Docker's much shorter
default, then requires a changed `StartedAt`, running state and returned HTTP
before reporting success
([HA Container installation](https://www.home-assistant.io/installation/linux/),
[Home Assistant Core Dockerfile](https://github.com/home-assistant/core/blob/dev/Dockerfile),
[Docker restart reference](https://docs.docker.com/reference/cli/docker/container/restart/),
[Docker container filters](https://docs.docker.com/reference/cli/docker/container/ls/);
accessed 2026-08-03; source tier: primary; confidence: high).

Docker restart policies are useful baseline crash recovery, but they do not
recover a still-running container whose API is hung. A host-side systemd timer
can make independent HTTP probes and invoke the narrow wrapper. Docker Autoheal
and DeUnhealth were rejected for this one-container deployment because they
require broad Docker-socket access and add a generic privileged controller;
the Docker daemon socket is effectively a host-control boundary
([Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/),
[Docker daemon attack surface](https://docs.docker.com/engine/security/),
[Autoheal](https://github.com/willfarrell/docker-autoheal),
[DeUnhealth](https://github.com/qdm12/deunhealth),
[systemd timers](https://manpages.ubuntu.com/manpages/noble/man5/systemd.timer.5.html);
accessed 2026-08-03; source tiers: primary documentation and project sources;
confidence: high for Docker/systemd, medium for third-party project fit).

SSH is a transport for one fixed operation, not an agent tool. The selected
design uses a dedicated non-admin account and key; a root-owned
`authorized_keys` entry combines `restrict`, a LAN `from=` condition and a
forced command. A separate `Match User` block requires public-key auth and
disables password, keyboard-interactive auth, forwarding, TTY, tunnels and user
startup files. The gate ignores the requested SSH command and executes only an
exact noninteractive `sudo` wrapper; sudoers grants that one pathname with no
environment preservation and no arguments. The client pins a pre-recorded
Ed25519 host key, rejects changed/unknown keys, disables agents/forwarding and
has no HA token
([OpenSSH sshd](https://man.openbsd.org/sshd.8),
[OpenSSH client configuration](https://man.openbsd.org/ssh_config),
[Ubuntu sshd_config](https://manpages.ubuntu.com/manpages/questing/man5/sshd_config.5.html),
[sudoers manual](https://www.sudo.ws/docs/man/1.9.14/sudoers.man.pdf);
accessed 2026-08-03; source tier: primary; confidence: high).

The local policy waits for a new non-baseline Core incident confirmed for five
minutes, repeats the frontend probe three times, and only then uses SSH. The
host wrapper serializes execution, respects a maintenance lock, allows one
start/restart per six hours and returns one fixed status token. The local
ledger permits at most three failed deliveries five minutes apart. The timer
is installed disabled: activation requires administrator bootstrap on the
Ubuntu target followed by a healthy end-to-end proof that returns
`healthy_no_action` and performs zero restarts.

## Exact Tuya DHCP Recovery and Upgrade Decision

The live HACS entities report LocalTuya `v5.2.5` current and Tuya Local
`2026.5.4` with `2026.7.2` available; the live Core reports `2026.5.2`
(local authenticated read-only probes, 2026-08-03; confidence: high). These two
integrations have materially different recovery behavior.

LocalTuya `v5.2.5` already owns its DHCP repair path. It listens on UDP ports
6666/6667, matches a broadcast by stable `gwId`, writes a changed host into its
own config entry, and its update listener reloads that entry. Its separate
60-second reconnect loop only retries the stored address, so `localtuya.reload`
alone is not an IP repair. Home Butler should observe and verify the
integration-owned update; it must not automate LocalTuya's multi-step device
edit flow or modify `.storage`
([LocalTuya v5.2.5 discovery/update source](https://github.com/rospogrigio/localtuya/blob/v5.2.5/custom_components/localtuya/__init__.py#L92-L152),
[UDP discovery source](https://github.com/rospogrigio/localtuya/blob/v5.2.5/custom_components/localtuya/discovery.py);
accessed 2026-08-03; source tier: primary; confidence: high). Because this path
depends on LAN broadcasts reaching the HA Container, VLAN, firewall or
container-network isolation can still prevent it; that limitation is an
inference from the exact UDP bind and the absence of active scanning in the
reconnect loop (confidence: high).

Tuya Local `2026.5.4` has no background relocation for existing entries. Its
supported options flow can change a host and validates the candidate device
before storing options, but the frontend config-flow endpoints are admin-only,
carry the local key in the form, are not a documented stable external API and
do not provide an atomic operational rollback. A plain reload reuses the same
stored host. Therefore Home Butler does not automate that internal flow over
plain LAN HTTP
([Tuya Local 2026.5.4 options flow](https://github.com/make-all/tuya-local/blob/2026.5.4/custom_components/tuya_local/config_flow.py#L567-L623),
[HA Core 2026.5.4 options-flow views](https://github.com/home-assistant/core/blob/2026.5.4/homeassistant/components/config/config_entries.py#L258-L291),
[official REST API](https://developers.home-assistant.io/docs/api/rest/);
accessed 2026-08-03; source tier: primary; confidence: high for code paths,
medium-high for external-contract risk).

Tuya Local `2026.7.2` adds a 60-second active scan for unreachable devices by
device ID and updates both data and overriding options before the normal entry
reload. Its release metadata requires Home Assistant `2026.6.0`, while the
current Core is `2026.5.2`. The safe sequence is consequently: verified host
backup and rollback plan → update the HA Container to a reviewed Core version
at least 2026.6 → verify HA/entities → update Tuya Local to exact 2026.7.2 →
verify automatic discovery. No part of this sequence is executed until the
prepared least-privilege host channel or equivalent administrator maintenance
access exists
([Tuya Local 2026.7.2 release](https://github.com/make-all/tuya-local/releases/tag/2026.7.2),
[rediscovery implementation](https://github.com/make-all/tuya-local/blob/2026.7.2/custom_components/tuya_local/helpers/discovery.py#L45-L160),
[HACS minimum Core version](https://github.com/make-all/tuya-local/blob/2026.7.2/hacs.json),
[Home Assistant Backup](https://www.home-assistant.io/integrations/backup/);
accessed 2026-08-03; source tier: primary; confidence: high).

A deployed private inventory preflight now records only the reviewed version
facts and one of `core_upgrade_required`, `backup_required_before_update`,
`automatic_ip_recovery_available`, or `review_required`. Unknown future
versions fail closed. This status is not a model mutation tool and contains no
Tuya IDs, keys, IP addresses or HA token. The same preflight uses the Core
2026.5.2 admin-only `backup/info` WebSocket command to verify that the manager
is idle, agent errors are empty, and a backup of the current Core includes both
Home Assistant and its database with no failed agents/folders/add-ons. A live
complete backup less than 24 hours old was found. Backup ID and name are
discarded; `restore_tested` remains false, so existence is not misreported as a
proven restore
([Core 2026.5.2 backup/info implementation](https://github.com/home-assistant/core/blob/2026.5.2/homeassistant/components/backup/websocket.py#L59-L101),
[Home Assistant Backup](https://www.home-assistant.io/integrations/backup/);
accessed 2026-08-03; source tier: primary; confidence: high).

## Installed SSH Command Integration Is Not a Recovery Boundary

The live HA instance has `gensyn/ssh_command` `v1.0.2` loaded and advertises
`v1.0.3`; its only service is `ssh_command.execute` (local authenticated
read-only service/config-entry inspection, 2026-08-04; confidence: high).
Version `v1.0.3` only adds an optional SSH port, so it does not add a bounded
recovery profile. The integration stores no target credentials in its empty
single-instance config entry. Every service call supplies an arbitrary host,
username, password or key path, command or input, host-key policy and timeout;
the coordinator passes those values to AsyncSSH and returns stdout/stderr.
Consequently it is a generic remote-shell primitive, not a safe substitute for
the prepared forced-command identity. It will not be exposed to the model or
used to bypass the owner's one-time bootstrap
([v1.0.3 service schema and validation](https://github.com/gensyn/ssh_command/blob/v1.0.3/__init__.py),
[v1.0.3 execution path](https://github.com/gensyn/ssh_command/blob/v1.0.3/coordinator.py),
[v1.0.3 release](https://github.com/gensyn/ssh_command/releases/tag/v1.0.3);
accessed 2026-08-04; source tier: primary project source plus live HA evidence;
confidence: high).

## Risks and Uncertainties

- Exact Tuya integration ownership and config-entry mappings are inventoried.
  LocalTuya 5.2.5 has integration-owned stable-ID IP repair; installed Tuya
  Local 2026.5.4 has reload only until the compatible 2026.7.2 upgrade is
  completed. Official cloud Tuya remains observation-only.
- Port 22 being open does not prove a safe recovery account exists. No SSH
  authentication or command was attempted. The absence of `hassio`/Supervisor
  from the live Core makes a dedicated least-privilege host/container recovery
  identity the remaining requirement.
- The installed `ssh_command.execute` service is deliberately excluded from the
  agent: its arbitrary target, credential and command fields would collapse the
  fixed-command security boundary.
- YandexStation local/cloud behavior varies by speaker model. The HA service
  accepted the live TTS call, but API acceptance cannot prove that a human
  actually heard the message.
- There are no directly comparable public end-to-end benchmarks; local fault
  injection tests define acceptance.

## Next Steps

1. Finish controlled fault-scenario verification without changing real device
   state unnecessarily.
2. Bootstrap the prepared dedicated forced-command SSH identity on the Ubuntu
   host, verify `healthy_no_action` with zero restarts, and only then enable the
   independent timer; never reuse a general admin shell.
3. After host access exists, create and verify backup/rollback, update HA Core
   from 2026.5.2 to the reviewed current stable `2026.7.4`, then update Tuya Local
   from 2026.5.4 to exact 2026.7.2 and verify its 60-second rediscovery.
4. Measure real incident/recovery latency over several outages and tune debounce
   only from evidence.
5. Install/configure Yandex Station Intents or the four exact manual scenarios,
   then verify one read-only phrase before any live switch phrase.
6. Build free Alice conversation as a separate private HTTPS/OAuth edge with a
   3–3.5 second local deadline and asynchronous YandexStation TTS fallback.
