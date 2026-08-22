# Preflight report

The diagnostics sections below are a Stage 1 snapshot. Later remediation and
stage-result sections supersede their statements about current disk capacity,
systemd, installed packages, and Ollama availability.

- Observed at: `2026-07-31T15:53:20+05:00`
- Project root: `/root/Jarvis`
- Artifact root: `/root/Jarvis/home-butler`
- Mode: read-only diagnostics; no packages or services changed

## Environment

| Check | Result |
|---|---|
| User | `root` (`uid=0`, groups: `root`) |
| Distribution | Ubuntu 22.04.5 LTS (Jammy) |
| Kernel | `6.6.87.2-microsoft-standard-WSL2` |
| Runtime | WSL2; PID 1 is `init(Ubuntu)`, not systemd |
| CPU | Intel Core i5-12400F, 6 cores / 12 threads |
| WSL memory | 7.7 GiB total, 7.2 GiB available at observation time |
| Swap | 2.0 GiB, unused |

This is not a native Ubuntu boot. The instructions assumed a normal Ubuntu host with systemd and direct AMD GPU access, but the inspected environment is WSL2.

## Storage

| Filesystem | Size | Used | Available | Notes |
|---|---:|---:|---:|---|
| WSL root (`/dev/sdd`, ext4) | 1007 GiB | 15 GiB | 941 GiB | Sparse virtual disk view |
| Windows `C:` | 120 GiB | 120 GiB | 461 MiB | 100% by rounded `df`; Ubuntu VHD is stored on this drive |
| Windows `D:` | 347 GiB | 277 GiB | 70 GiB | Mounted read-only observation only |
| Windows `E:` | 233 GiB | 26 GiB | 208 GiB | Mounted read-only observation only |
| Windows `F:` | 120 GiB | 15 GiB | 105 GiB | Mounted read-only observation only |
| Windows `G:` | 932 GiB | 215 GiB | 718 GiB | Mounted read-only observation only |
| Windows `H:` | 119 GiB | 22 GiB | 98 GiB | Mounted read-only observation only |

The apparent 941 GiB free inside ext4 does not prove that the sparse WSL virtual disk can grow: the backing distribution is on `C:`, where only about 461 MiB is free. The current official Ollama Linux archive alone is 1,422,353,729 bytes (release `v0.32.5`), before extraction or any model download. Installation cannot succeed safely in the current storage state.

## GPU and acceleration

- Windows reports `AMD Radeon RX 6600 XT` with driver `32.0.21043.7012`.
- WSL exposes a Microsoft `3D controller` using `dxgkrnl` and `/dev/dxg`.
- WSL includes `libd3d12.so`, `libd3d12core.so`, and `libdxcore.so`.
- `/dev/dri` is absent.
- No `amdgpu` kernel driver is active inside WSL; the filtered kernel log contains only generic DRM initialization.
- `vulkaninfo`, `rocminfo`, and `vulkan-tools` are absent.
- Direct RX 6600 XT VRAM availability to Linux/Ollama is therefore not proven.

Conclusion: assume CPU fallback until the installed Ollama version demonstrates a supported backend in its own logs. Do not apply ROCm overrides or `HSA_OVERRIDE_GFX_VERSION`.

## Services

- systemd is not the init system, so `systemctl --failed` cannot connect to a bus.
- `ollama` is absent; no Ollama process is running and `127.0.0.1:11434` refuses connections.
- A systemd-managed Ollama or Hermes service cannot work until systemd is deliberately enabled for WSL and WSL is restarted. No restart was performed.

## Tool availability

| Tool | Status |
|---|---|
| git | present, 2.34.1 |
| curl | present, 7.81.0 |
| wget | present, 1.21.2 |
| python3 | present, 3.10.12 |
| pip | present, 22.0.2 |
| node | present, 22.23.1 |
| npm | present, 10.9.8 |
| jq | present, 1.6 |
| docker | absent |
| podman | absent |
| vulkaninfo | absent |
| rocminfo | absent |
| ollama | absent |
| sensors | command absent; package state needs repair/verification |
| smartctl | absent |

## Suitability assessment

- CPU: suitable for a small quantized 2B-4B model, but response speed must be measured.
- RAM: 7.7 GiB available to WSL is materially below the expected 16 GiB host RAM. A quantized 4B model may fit with a 4096 context, but headroom for Hermes is tight. A 2B-3B fallback may be required.
- Swap: present (2 GiB), but swap is not a substitute for model RAM and may reduce responsiveness.
- GPU: physical RX 6600 XT is confirmed on Windows, but direct Linux `amdgpu`/Vulkan access is not available in the inspected WSL state.
- Disk: model installation is blocked by critically low free space on the WSL backing drive `C:` despite the large logical ext4 size.
- Services: auto-start requirements are blocked because systemd is disabled and the only Ubuntu account observed is `root`; the target design requires an ordinary user service.

## Safe package group prepared, not executed

Installed already: `curl`, `git`, `jq`. Missing or unusable diagnostic tools can be addressed later with one reviewed apt group:

```bash
apt-get update
apt-get install --no-install-recommends mesa-vulkan-drivers vulkan-tools lm-sensors smartmontools
```

This group changes the operating system and must not run before owner confirmation. It should also wait until adequate free space exists on `C:`.

## Stage 1 result

Stage 1 diagnostics completed. Before Ollama installation, the owner must free sufficient space on `C:` (at least 10 GiB is recommended for the Ollama archive, extraction, one small model, and operational headroom) and decide whether this WSL2 environment is the intended deployment target. Enabling systemd would require editing `/etc/wsl.conf` and restarting WSL, both outside the current read-only stage and subject to explicit confirmation.

## Storage remediation

Completed at `2026-07-31` after explicit owner approval:

- the `Ubuntu` WSL2 distribution was moved with `wsl --manage Ubuntu --move` to `H:\\WSL\\Ubuntu`;
- the registered distribution storage drive is now `H:`;
- a pre-move VHDX backup was created and verified at `H:\\WSL-Backups\\Ubuntu-before-move-2026-07-31.vhdx`, then permanently deleted after a separate explicit owner instruction and a successful post-move boot check;
- backup SHA-256: `6831F54A84245C4F32660C71271FCC1B159191FBA38F6DA86F607573B4BCE395`;
- the first moved VHDX acquired an unsupported `Application Protected` attribute and could not be mounted;
- that file is preserved as `H:\\WSL\\Ubuntu\\ext4.vhdx.application-protected`;
- the working VHDX was restored by copying the verified unencrypted backup and reapplying the WSL distribution ACL;
- before deletion, the working VHDX SHA-256 exactly matched the verified backup;
- Ubuntu starts successfully, `/root/Jarvis` is present, and all required project files were verified;
- 77.83 GiB was free on `H:` during the final storage check after backup deletion and Ollama installation.

The `C:` capacity blocker is resolved for Ubuntu without browsing or cleaning any Windows user profile. The protected, non-working first move result is still isolated at `H:\\WSL\\Ubuntu\\ext4.vhdx.application-protected`; it is not used by WSL and was not deleted without a separate exact-target decision.

## Stage 2 result: Ollama and local runtime

Completed at `2026-07-31` after the approved WSL restart and service installation:

- systemd is enabled through `/etc/wsl.conf`; PID 1 is systemd and `systemd-logind` is active;
- Ollama `0.32.5` is installed from the reviewed official Linux installer and runs as the dedicated `ollama` system user;
- the service is enabled and active;
- the API responds at `http://127.0.0.1:11434/api/version`;
- the listening socket is restricted to `127.0.0.1:11434`;
- `OLLAMA_NO_CLOUD=1` is active and the journal confirms `Ollama cloud disabled: true`;
- concurrency is limited to one request and one loaded model, with a 4096-token context;
- Ollama detected only its CPU backend: 7.7 GiB total, about 7.1 GiB available, and 0 B VRAM;
- Vulkan tools see only the software `llvmpipe` device, not the Windows RX 6600 XT, so no ROCm/Vulkan override was applied;
- no model has been downloaded yet; model selection and evaluation belong to Stage 3.

The tracked service override is `/root/Jarvis/home-butler/config/ollama.service.override.conf` and matches the installed systemd drop-in.

### Known WSL systemd limitation

Independent verification reproduced an intermittent WSL user-session race when
several Windows-side `wsl.exe` invocations start simultaneously. In that case,
`user@0.service` can exit with `219/CGROUP`, even though a later sequential
session starts successfully. Two sequential final checks showed the user manager,
`systemd-logind`, and Ollama active with zero failed units. Microsoft tracks the
same user-session warning for WSL 2.6.x, Ubuntu 22.04, and the 6.6.87.2 kernel in
the open issue <https://github.com/microsoft/WSL/issues/13826>.

This does not block the system-level Ollama service, but it is a mandatory risk
check for the later Hermes user-service stage. Parallel `wsl.exe` launches are
avoided during normal project execution.

### Windows PATH isolation

The official base Ollama unit captured Windows paths during installation, but the
installed project override completely replaces its effective `PATH` with Linux-only
directories. Interactive WSL shells still inherit the Windows PATH at this stage;
all project commands therefore set a Linux-only PATH explicitly. Disabling global
Windows-PATH inheritance is prepared as a separate WSL configuration change and
must be applied only with an approved WSL restart.

## Windows system-temporary cleanup

Completed at `2026-07-31` after explicit owner approval, with the scope limited
to system-managed temporary data on `C:`:

- verified and cleaned only `C:\\Windows\\Temp` and the system WER directories
  `ReportQueue`, `ReportArchive`, and `Temp` below
  `C:\\ProgramData\\Microsoft\\Windows\\WER`;
- ran the Windows `Delete-DeliveryOptimizationCache` maintenance command
  successfully;
- did not enumerate or access any path below `C:\\Users`, including the profile
  `C:\\Users\\Елена4`;
- did not touch personal files, Recycle Bin contents, `Windows.old`, the Windows
  Update download store, or stop/restart Windows services;
- all four explicitly permitted directories contained zero files before and
  after cleanup;
- free space reported for `C:` after cleanup was `168,816,640` bytes (about
  161 MiB), so the capacity pressure is not caused by the permitted temporary
  directories.

Further analysis or cleanup of `C:` requires a separately approved scope. The
project and active Ubuntu WSL virtual disk remain on `H:`.

### Extended system cleanup

After a further explicit approval, Windows component cleanup and an exact list
of verified system leftovers were processed without entering `C:\\Users`:

- DISM component cleanup completed successfully and reclaimed 2,150,400 bytes;
- an obsolete Microsoft Edge version and its update download cache were removed
  only after the current Edge version was verified;
- ten inactive driver packages were removed only after confirming that none was
  referenced by an active signed PnP driver; no force option was used;
- the exact Edge/driver cleanup reclaimed 2,331,484,160 bytes;
- current free space on `C:` is 2,302,312,448 bytes (2.14 GiB).

The requested 10 GiB target has not been reached. No additional temporary or
reclaimable system cache of that size exists outside user profiles. The remaining
large directories are installed Autodesk, Adobe, Office, and Kaspersky program
data, not temporary files. `DISM /ResetBase` was deliberately not used because it
would permanently remove the ability to uninstall currently installed Windows
updates. Personal files, the Recycle Bin, and every path below `C:\\Users` remain
untouched.

## Stage 5 result: Hermes Agent installation

Completed at `2026-07-31`:

- installed Hermes Agent package `0.19.1` from the pinned upstream revision
  `7b5a18817e5952a1f4d60edd30fc034c10eb16e3`;
- the repository installer exactly matched the previously reviewed copy with
  SHA-256 `ab3e6ae1a1bda828941df8911ae44ed5de68412805124f338f157aa0360eb660`;
- source, virtual environment, configuration, runtime data, and package caches
  are kept below `/root/Jarvis/home-butler`, with only the standard Linux CLI
  launchers in `/root/.local/bin`;
- installed Python `3.11.15` through verified `uv 0.11.32` and the Ubuntu
  `ripgrep` package; `ffmpeg` was already present;
- dependencies were installed from the hash-verified `uv.lock`;
- browser/Chromium installation, interactive setup, cloud credentials, and
  bundled skill seeding were skipped;
- Hermes Doctor reports a valid Python environment, consistent version files,
  no active Hermes security advisories, no suspicious MCP commands, and no
  configured cloud provider credentials;
- `.env` and `config.yaml` permissions are `0600`, telemetry sharing is off,
  and the config schema is current at version 33.

## Stage 6 result: local Ollama connection

Completed at `2026-07-31` after the explicitly approved Ollama restart:

- the local provider is `local-ollama`, model `home-butler`, endpoint
  `http://127.0.0.1:11434/v1`, with no cloud fallback or API key;
- `reasoning_effort: none` prevents thinking-only responses on the OpenAI wire;
- the service is restricted to localhost and uses a 64,000-token default,
  Flash Attention, `q8_0` KV-cache quantization, one parallel request, and one
  loaded model;
- the initial `qwen3:1.7b` base was replaced because its architecture capped the
  real context at 40,960 tokens;
- the active base is `qwen3.5:2b-q4_K_M`: 2.3B parameters, Q4_K_M, Apache 2.0,
  262,144-token architectural context, tools, vision, and thinking support;
- `home-butler` is configured for 64,000 tokens and `ollama ps` confirmed an
  actual 64,000-token runner, 2.4 GB loaded size, 100% CPU, and no GPU backend;
- five model regressions passed: Russian response, no fabricated metric, strict
  structured JSON, safe tool selection, and prompt-injection refusal;
- a real Hermes one-shot request through the project profile returned a Russian
  answer through the local provider in 29.84 seconds;
- the superseded `qwen3:1.7b` tag was removed only after all replacement tests
  passed.

Stage 6 is complete. Stage 7 (the Butler workspace and policy files) is next.

## Stages 7–8 result: workspace and read-only skills

Completed at `2026-07-31`:

- created the project `AGENTS.md`, `HEARTBEAT.md`, `SOUL.md`, `TOOLS.md`, the
  single project README, security policy, and secret-free configuration examples;
- created five local skills: `home-health-audit`, `diagnose-home-assistant`,
  `diagnose-internet`, `diagnose-mqtt`, and `diagnose-zigbee2mqtt`;
- each skill records `mode: read-only`, `risk: low`, and
  `requires_approval: false` under validated metadata;
- all five skill folders pass the official skill structure validator;
- Hermes lists all five as enabled local skills through project-owned links in
  its isolated profile;
- independent review and three dry scenarios confirmed that the skill text does
  not invent missing data or recommend mutation.

## Stage 9 result: fail-closed local health report

The executable `scripts/local-health-check.sh` is read-only, uses a Linux-only
PATH, has bounded local requests, excludes Windows/DrvFS and virtual snap mounts,
and returns valid JSON. Its verified output includes CPU, RAM, swap, the Linux
ext4 filesystem, temperatures when available, failed systemd units, local Ollama,
Hermes installation/gateway status, and `home_assistant: not_configured`.

Shell syntax and `jq` schema assertions pass. At the final observation
(`2026-08-02T01:37:54+05:00`) Ollama `0.32.5` was reachable, Hermes was
installed, `user@0.service` was reported failed, and neither Hermes Gateway nor
Home Assistant was configured.

The earlier free-form model-report subtest remains rejected: both tested small
models added unsupported claims. Stage 9 therefore implements a fail-closed
adapter rather than accepting model prose. Ollama receives a closed JSON Schema
containing only opaque fact/problem/missing identifiers. The adapter requires
the exact identifier sets, verifies model identity and completion, enforces an
overall request deadline, rechecks snapshot freshness after inference, then
renders Russian text only from trusted templates and validated fields. Model
failures cannot produce `HEARTBEAT_OK`.

Completed at `2026-08-02`:

- shell syntax and live `jq -e` parsing passed;
- the 42-test offline suite passed with 41 successes and the opt-in live test
  skipped; it covers malformed input, probe inconsistency,
  exact-set failures, injection-shaped sensor names, deterministic rendering,
  full CLI exit/stdout/stderr behavior, and collector JSON fixtures;
- a four-call live regression passed for repeated current data, synthetic clean
  data, and synthetic problem data;
- the strengthened five-case local-model evaluation passed 5/5;
- the direct collector-to-reporter command returned an evidence-backed
  `ТРЕБУЕТСЯ ВНИМАНИЕ` for `user@0.service`, while explicitly preserving
  unavailable temperature data;
- the `home-health-audit` skill now returns adapter output byte-for-byte and
  passed the official skill validator.

No service, WSL instance, model definition, or configuration was restarted or
changed during Stage 9 verification. A report inference can keep the selected
model resident in Ollama for five minutes; this is the only observable runtime
side effect.

## Stage 12 result: Hermes security boundary

Completed before enabling any autonomous service:

- `approvals.mode` is `manual` and `approvals.cron_mode` is `deny`;
- explicit deny rules block deletion, shutdown/reboot, service lifecycle,
  container shutdown, filesystem formatting, raw disk writes, firewall changes,
  SSH/SCP/remote rsync, MQTT publish, HA service calls, and curl POST;
- `agent.disabled_toolsets` disables terminal, file writes, browser, web,
  cron, code execution, delegation, Home Assistant control, skill mutation,
  memory, messaging-adjacent generation, and kanban;
- the effective CLI surface contains only `clarify`;
- Telegram, Discord, WhatsApp, Slack, Signal, Home Assistant, QQBot, Yuanbao,
  Teams, and Google Chat resolve to zero model-facing toolsets;
- every platform has the `no_mcp` sentinel, preventing future MCP servers from
  being added implicitly;
- a direct guard-function test confirmed every required deny example is blocked;
- `--yolo` is not configured and dangerous child-agent commands auto-deny.

The built-in Hermes Home Assistant toolset remains disabled because it contains
`ha_call_service` and has no entity allowlist. Stage 10 must use a separate
GET-only adapter after the owner provides configuration choices without placing
the token in chat.

The `clarify`-only CLI statement above records the Stage 12 baseline. Stage 10
later added one local-CLI-only MCP tool, `ha_get_snapshot`; every gateway kept
the `no_mcp` sentinel and still resolves to zero model-facing toolsets.

## Stage 10 result: Home Assistant read-only snapshot

Completed at `2026-08-02`:

- the origin is pinned to `http://192.168.1.127:8123`; transport is bounded
  GET-only with redirects and environment proxies disabled;
- the token and HA config are regular root-owned `0600` files with one hardlink;
- owner-only registry discovery identified the approved Tuya entities, while the
  model receives only a sanitized exact allowlist of eight entity IDs and no
  attributes;
- the model-facing MCP surface contains only `ha_get_snapshot`; host-only adapter
  diagnostics retain list and point-read operations;
- live Hermes proof session `20260802_064139_0cff7e` used model `home-butler`
  through local Ollama, made the exact structured snapshot call, and was capped
  at one model iteration so no free-form sensor summary could be accepted;
- the live result was `stale_data`: eight allowed, four available, and four
  unavailable entities. This is a sensor-state result, not a connection failure;
- the 67-test offline suite passed with 66 successes and one opt-in live test
  skipped; MCP discovery found exactly one tool and the skill validator passed;
- independent code and security audits found no P0-P2 implementation issue after
  the model-facing surface was reduced to the snapshot.

The structured MCP result is the evidence source. An earlier unrestricted
second model turn added unsupported interpretation, so free text is not treated
as a measurement and must remain behind deterministic validation in a future UI.

## Stage 15 result: no-cloud verification

Verified at `2026-07-31` without printing secret values:

- OpenAI, OpenRouter, and Anthropic API keys are absent;
- the selected model is `home-butler` through `local-ollama`;
- Ollama listens only on `127.0.0.1:11434` and has `OLLAMA_NO_CLOUD=1`;
- no Hermes gateway is listening;
- at the time of this Stage 15 snapshot, the Home Assistant token was not yet
  configured; Stage 10 above records the later local configuration;
- Hermes `.env` and `config.yaml` both have mode `0600`;
- no cloud fallback is configured.

## GPU backend re-evaluation: supported CPU fallback

Verified at `2026-08-02` without changing packages, services, WSL, or drivers:

- Windows still reports `AMD Radeon RX 6600 XT` (`PCI 1002:73ff`) with driver
  `32.0.21043.7012` dated `2026-04-28`;
- WSL exposes the Microsoft `dxgkrnl` bridge and `/dev/dxg`, but no `/dev/kfd`,
  `/dev/dri`, or DRM render node;
- `vulkaninfo --summary` exposes only the CPU device `llvmpipe` and does not
  expose the RX 6600 XT;
- the Ollama `0.32.5` installation contains CPU, CUDA, and Vulkan runners but no
  ROCm runner; its journal selects `library=cpu` and reports only host memory;
- the current Ollama hardware page requires ROCm v7 on Linux and does not list
  RX 6600 XT in either its Linux or Windows supported AMD tables:
  <https://docs.ollama.com/gpu>;
- AMD's ROCm 7.2 WSL matrix and the production ROCDXG matrix also omit RX 6600
  XT: <https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/compatibilityrad/wsl/wsl_compatibility.html>
  and <https://github.com/ROCm/librocdxg>;
- AMD's Windows table identifies this card as `gfx1032` with runtime support but
  without full HIP SDK support; Ollama's own Windows table remains the deciding
  compatibility boundary: <https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/shared/hipsdk/reference/system-requirements.html>;
- the installed systemd drop-in is byte-identical to the tracked override,
  explicitly keeps `OLLAMA_VULKAN=0`, and contains no ROCm/GFX override.
- a fresh local inference completed successfully; `ollama ps` then reported
  `100% CPU`, context `4096`, and the health collector reported
  `size_vram_bytes=0`. Eight test tokens took about `0.328` seconds after model
  loading, confirming a functional CPU backend rather than a hidden GPU path.

Conclusion: the only supported project backend on this WSL host is CPU fallback.
Enabling Vulkan would select software `llvmpipe`, while ROCm would require an
unsupported GPU/WSL path or forbidden GFX spoofing. No daemon reload or Ollama
restart is needed because the safe configuration was already active.

## GPU backend re-evaluation: native Windows Vulkan selected

This result supersedes only the final CPU-only conclusion immediately above;
the recorded limitation of direct GPU access from WSL remains valid.

Applied and verified at `2026-08-02` with owner approval:

- the official signed Ollama `0.32.5` installer was verified against the
  published SHA-256 and installed at `H:\Ollama`; the copied model store is
  `H:\OllamaModels`;
- the Windows server is bound only to the current internal WSL vEthernet address
  `172.27.192.1:11434`. It is not bound to `0.0.0.0` or a LAN address;
- the Ollama installer nevertheless registered a persistent Hyper-V inbound
  allow rule for TCP/11434 with any VM creator, local address, and remote
  address. No direct firewall command was issued during this work, and the
  exact socket bind prevents current LAN listening, but removing or narrowing
  the rule requires separate owner approval;
- `OLLAMA_NO_CLOUD=1`, one loaded model, one parallel request, Flash Attention,
  `q8_0` KV cache, and a 64,000-token default are configured. Vulkan is left at
  its supported default; `HSA_OVERRIDE_GFX_VERSION` and Vulkan device overrides
  are unset;
- the server log identifies `library=Vulkan`, `AMD Radeon RX 6600 XT`, 8.0 GiB
  total GPU memory, and `26/26` layers offloaded. A ROCm-driver warning is not
  the selected path and does not prevent the confirmed Vulkan backend;
- a 64,000-context inference produced `ollama ps: 100% GPU`; `/api/ps` reported
  `size=2405810829`, `size_vram=2405810829`, and `context_length=64000`;
- the 64-token measurement produced about `45.88 tokens/s` after loading,
  compared with the preceding CPU proof of about `24.4 tokens/s`;
- the one endpoint value is stored in the root-owned `0600` Hermes environment
  file. Hermes expands it, while the collector, renderer, and evaluator use a
  shared loader that rejects stale WSL gateways, wildcard/LAN targets,
  duplicates, and unsafe file metadata;
- Hermes, `local-health-check.sh`, `health_report.py`, and the canonical
  five-scenario evaluator now use the internal Windows endpoint. The evaluator
  passed 5/5 at about 47.7–49.7 tokens/s and `/api/ps` proved full VRAM use;
- the offline suite passes all 69 enabled tests with one opt-in live test
  skipped; the same live test passes when enabled. All TypeScript workspace
  typechecks, Hermes Doctor, and one-tool Home Assistant MCP discovery pass;
- the Linux systemd Ollama remains unchanged on `127.0.0.1:11434` as a safe CPU
  rollback path.

The vEthernet address is private but can change after a complete WSL restart.
Before enabling a persistent user service, compare it with
`ip -4 route show default` and update both the Windows bind and project endpoint
if needed. Do not replace it with a wildcard or LAN bind.

## Final report: stages 1–16 complete

Final verification date: `2026-08-02`.

### 1. Что установлено

| Компонент | Фактическая версия/модель |
|---|---|
| Ubuntu | `22.04.5 LTS` in WSL2 |
| Kernel | `6.6.87.2-microsoft-standard-WSL2` |
| Linux Ollama | `0.32.5`, loopback CPU fallback |
| Windows Ollama | `0.32.5`, signed native build in `H:\Ollama` |
| Hermes Agent | `0.19.1 (2026.7.30)`, upstream `0a62610f` |
| Base model | `qwen3.5:2b-q4_K_M`, 2.3B, Q4_K_M |
| Local model | `home-butler:latest` |

Hermes uses the isolated `/opt/home-butler` copy and Python `3.11.15`.

### 2. Как работает модель

- Primary accelerator: AMD Radeon RX 6600 XT through native Windows Vulkan;
  all layers are offloaded. Direct WSL GPU access remains unavailable, so no
  ROCm/GFX spoofing is used.
- Final 64K proof: `context_length=64000`, `size=2405810829`,
  `size_vram=2405810829`, fully on GPU.
- Canonical five-test speed: 45.42–46.15 tokens/s warm; cold first test 6.98 s
  including a 4.48 s model load.
- At the 64K observation, Windows `llama-server` used about 1.2 GiB working set
  and 4.0 GiB private memory; model/KV VRAM allocation was about 2.4 GB.
- Linux Ollama on `127.0.0.1:11434` is the only fallback. Runtime selection
  probes the exact current WSL gateway and never falls back to LAN/cloud.

### 3. Какие сервисы запущены

| Unit/task | Status | Identity/purpose |
|---|---|---|
| `ollama.service` | active, enabled | Linux loopback CPU fallback, UID 999 |
| `home-butler.service` | active, enabled | system unit, `User=homebutler`, UID/GID 998 |
| `home-butler-heartbeat.timer` | active, enabled | 10 min + up to 30 s jitter |
| `home-butler-heartbeat.service` | successful oneshot | read-only health pipeline |
| `home-butler-startup-ha-check.timer` | active, enabled | one initial model-selected HA GET after WSL boot |
| `home-butler-startup-ha-check.service` | successful oneshot | exact HA fact, zero service calls |
| `home-butler-ha-proof.service` | successful manual oneshot | strict Ollama/HA/GPU proof |
| Windows task `Home Butler WSL Runtime` | Running (`0x41301`) | limited user; WSL keepalive as Linux UID 998 |
| Windows task `Home Butler Ollama GPU` | Running (`0x41301`) | interactive limited user, exact WSL bind supervisor |

The literal preferred user-manager service was replaced by a system-scope unit
with `User=homebutler`. This is more reliable at WSL activation while the agent
process remains non-root. Test G changed MainPID from `5608` to `6785` and the
service returned to active/running with the timer still active.

### 4. Home Assistant

- configured: yes;
- fixed local endpoint: `192.168.1.127:8123`, reachable;
- allowlist: eight owner-approved Tuya entities;
- current result: four available, four `unavailable`, aggregate `stale_data`;
- access method: custom bounded HTTP client, fixed `/api/` or `/api/states`,
  `GET` only, no redirects/proxy environment;
- model tool: only `mcp__home_assistant_read__ha_get_snapshot`;
- service calls/state writes: not implemented and not exposed;
- token: root-owned file mode `0600`, passed to units with `LoadCredential`;
  not copied into `/opt` and not printed.

The final model proof returned the exact sanitized fact:

```json
{
  "entity_id": "binary_sensor.24g_presence_sensor_v3_dvizhenie",
  "state_kind": "enum",
  "state_value": "off",
  "observed_at": "2026-08-02T16:51:59+00:00",
  "source_last_updated_at": "2026-08-02T16:41:01+00:00",
  "source": "Home Assistant via ha_get_snapshot"
}
```

The same proof recorded `http_method=GET`, `service_calls=0`, and
`fully_on_gpu=true`.

### 5. Безопасность

- No OpenAI, OpenRouter or Anthropic key; cloud fallback absent; local model
  and private/loopback endpoints only.
- Windows Ollama listens only on the exact internal WSL address; Linux Ollama
  only on loopback. Neither Home Butler nor Hermes opens a TCP listener.
- SSH was not granted or changed; no root SSH action was configured.
- Hermes globally disables terminal, file, browser, cronjob, code execution,
  delegation, built-in Home Assistant, web/search and other write-capable
  toolsets. CLI has only `clarify` plus the GET-only MCP snapshot.
- Approvals are manual, timeout 300 s; cron approvals are `deny`; destructive
  slash commands and MCP reload require confirmation. The agent does not rely
  on approvals for forbidden tools because those tools are absent.
- `Restart=on-failure` applies only to the agent process supervisor. The model
  has no restart/reboot capability; heartbeat performs no recovery.
- Secrets directory is `0700`; token is `0600`; runtime/state are isolated.
  `.gitignore` excludes secrets, and no secret content is present in the
  documented/source artifacts.
- `systemd-analyze security`: `3.9 OK` for gateway, heartbeat, HA proof and
  the initial startup HA check.
- The Windows supervisor validates Authenticode and only manages the pinned
  `H:\Ollama\ollama.exe serve` process. It rejects wildcard/LAN binds.

Residual: the Ollama installer created the broad Hyper-V rule
`Ollama 11434 Inbound`; changing it was not authorized. Exact socket binding
prevents present LAN listening. An unrelated pre-existing Mosquitto listener
on WSL port 8888 was observed but not modified.

### 6. Тесты

| Test | Result |
|---|---|
| A — локальная модель отвечает по-русски | PASS |
| B — Hermes использует `home-butler` и runtime policy | PASS |
| C — компьютер проверяется по реальным показателям | PASS |
| D — prompt injection помечается недоверенной | PASS |
| E — reboot компьютера/роутера автоматически отклонён | PASS |
| F — Ollama вызвала HA snapshot, показала точный state/time/source, без service call | PASS |
| G — перезапущен только собственный агент и снова доступен | PASS |

Automated evidence:

- full offline discovery: 104 tests, 103 passed, one expected opt-in live skip;
- live collector/model/renderer: 1/1 passed in 26.5 s;
- canonical model evaluator: 5/5 passed;
- HA model proof: exit 0, `verified=true`;
- runtime policy: `RUNTIME_POLICY_OK`;
- failed systemd units: none at final observation.

### 7. Созданные файлы

Secret contents, caches, vendored Hermes and temporary download artifacts are
intentionally omitted from this tree:

```text
home-butler/
├── AGENTS.md
├── HEARTBEAT.md
├── README.md
├── SECURITY.md
├── SOUL.md
├── TOOLS.md
├── config/
│   ├── home-assistant.env
│   ├── home-assistant.example.env
│   ├── inventory.example.yaml
│   ├── ollama.service.override.conf
│   ├── wsl.conf.proposed
│   └── systemd/
│       ├── home-butler.service
│       ├── home-butler-heartbeat.service
│       ├── home-butler-heartbeat.timer
│       ├── home-butler-startup-ha-check.service
│       ├── home-butler-startup-ha-check.timer
│       └── home-butler-ha-proof.service
├── hermes/
│   ├── .env
│   ├── .no-bundled-skills
│   ├── SOUL.md
│   └── config.yaml
├── models/home-butler.Modelfile
├── reports/preflight.md
├── scripts/
│   ├── configure-home-assistant-secret.sh
│   ├── health_report.py
│   ├── health_report_core.py
│   ├── heartbeat.py
│   ├── home_assistant_mcp.py
│   ├── home_assistant_read.py
│   ├── install-home-butler-service.sh
│   ├── install-windows-home-butler-tasks.ps1
│   ├── local-health-check.sh
│   ├── model_ha_proof.py
│   ├── no_cloud_audit.py
│   ├── ollama_endpoint.py
│   ├── run-hermes-gateway.sh
│   ├── verify-runtime-policy.py
│   └── windows-ollama-supervisor.ps1
├── skills/
│   ├── diagnose-home-assistant/SKILL.md
│   ├── diagnose-internet/SKILL.md
│   ├── diagnose-mqtt/SKILL.md
│   ├── diagnose-zigbee2mqtt/SKILL.md
│   └── home-health-audit/SKILL.md
└── tests/
    ├── evaluate_model.py
    ├── model-evaluation.md
    ├── test_evaluate_model_safety.py
    ├── test_health_report.py
    ├── test_health_report_deadline.py
    ├── test_health_report_live.py
    ├── test_heartbeat.py
    ├── test_home_assistant_mcp.py
    ├── test_home_assistant_read.py
    ├── test_local_health_check.py
    ├── test_model_ha_proof.py
    ├── test_no_cloud_audit.py
    ├── test_ollama_endpoint.py
    ├── test_service_definition.py
    ├── test_windows_home_butler_startup.py
    └── test_windows_ollama_supervisor.py
```

### 8. Команды эксплуатации

```bash
# Linux CPU fallback
ollama list
ollama ps
systemctl status ollama.service --no-pager
journalctl -u ollama.service -n 100 --no-pager

# Local non-root agent (system scope with User=homebutler)
systemctl status home-butler.service --no-pager
journalctl -u home-butler.service -n 100 --no-pager

# Hermes diagnostics against project config
cd /root/Jarvis/home-butler
HERMES_HOME=/root/Jarvis/home-butler/hermes \
  ./hermes-agent/venv/bin/hermes doctor
HERMES_HOME=/root/Jarvis/home-butler/hermes \
  ./hermes-agent/venv/bin/hermes tools --summary

# Manual safe heartbeat
systemctl start home-butler-heartbeat.service
journalctl -u home-butler-heartbeat.service -n 40 --no-pager

# Verified Ollama → HA → GPU example
systemctl start home-butler-ha-proof.service
journalctl -u home-butler-ha-proof.service -n 60 --no-pager -o cat

# Tests/audits
./scripts/no_cloud_audit.py
./scripts/local-health-check.sh | ./scripts/health_report.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
RUN_LIVE_OLLAMA=1 python3 -m unittest discover -s tests \
  -p 'test_health_report_live.py' -v
```

Windows GPU status:

```powershell
Get-ScheduledTask -TaskName 'Home Butler Ollama GPU'
Get-ScheduledTask -TaskName 'Home Butler WSL Runtime'
Get-NetTCPConnection -State Listen -LocalPort 11434
& 'H:\Ollama\ollama.exe' ps
```

### 9. Что осталось на следующий этап

No required implementation remains for the original plan. Optional future
work, each requiring a new scope/permission:

- expand the HA allowlist after owner review;
- add Telegram or another messaging channel with an explicit user allowlist;
- introduce read-only SSH under a separate `homeops` identity;
- design the first bounded recovery playbook and circuit breaker;
- add UPS/power monitoring and router monitoring;
- investigate MQTT/Zigbee health and only later consider approved recovery;
- remove or narrow the broad Hyper-V firewall rule;
- audit the unrelated Mosquitto listener on port 8888.

## Stage 13 boot-persistence follow-up (2026-08-03)

The earlier systemd enablement was necessary but insufficient in WSL because
background systemd services do not by themselves guarantee that the per-user
WSL VM stays alive after the launching `wsl.exe` command exits. The completed
Windows-to-WSL startup path is now:

1. After the owner signs in, limited task `Home Butler WSL Runtime` starts the
   fixed Ubuntu distribution and runs only `/usr/bin/sleep infinity` as Linux
   user `homebutler` (observed UID 998).
2. Limited task `Home Butler Ollama GPU` starts the pinned signed Ollama
   supervisor on the exact internal WSL address.
3. Enabled systemd units start Linux Ollama, the non-root Hermes gateway and
   timers. The endpoint guard prefers GPU for 45 seconds and then permits only
   the loopback CPU fallback.
4. The regular heartbeat runs after one minute and every ten minutes. A second
   one-shot boot timer makes the Ollama model itself call `ha_get_snapshot`.

Runtime evidence: both Windows tasks were `Running` with result `0x41301`,
limited privilege and a bounded five-restart policy. The WSL boot ID stayed
`0b2f472f-bbfb-43fa-ad82-32b804dd6711` across separate probes. Linux showed
`/usr/bin/sleep infinity` as UID 998. `home-butler.service`, both timers and
`ollama.service` were active/enabled; the gateway had `NRestarts=0` and selected
`http://172.27.192.1:11434`.

The initial model HA check completed successfully with four available and four
unavailable allowlisted entities, exact fact
`binary_sensor.24g_presence_sensor_v3_dvizhenie = off`, observation
`2026-08-03T03:50:09+00:00`, source update
`2026-08-03T03:45:45+00:00`, `http_method=GET`, `service_calls=0`, and
`fully_on_gpu=true`. Offline regressions were `104` tests with `103` passes and
one expected live skip; the separate live test passed in 26.5 seconds.

The trigger is intentionally per-user `AtLogOn`, not pre-login `SYSTEM`,
because this WSL distribution is registered to the Windows owner and is not
available in the SYSTEM account's WSL registry. A full PC reboot was not
performed; task execution, WSL persistence, service recovery and both initial
model checks were verified without terminating unrelated WSL workloads.
