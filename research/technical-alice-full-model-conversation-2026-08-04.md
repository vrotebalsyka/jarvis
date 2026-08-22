---
topic: "Full arbitrary conversation between Yandex Alice speakers and local Home Butler/Ollama"
type: "technical"
goals: "Determine whether scenario-free arbitrary speech can reach the local model; compare official Yandex Dialogs with local speaker interception; select and implement the safest viable architecture."
date: "2026-08-04"
methodology: "Parallel primary-source research, inspection of the deployed Home Assistant/Yandex Station integration, and local latency benchmarks. Confidence: High / Medium / Low."
---

# Research Report — Full Alice ↔ local-model conversation

> **Constraints:** no per-phrase Yandex scenarios; arbitrary multi-turn Russian dialog; local Ollama/Home Butler; spoken reply on the same Alice surface; no unrestricted HA or shell tools.
>
> **Assumption:** one activation phrase such as «Алиса, запусти навык Домашний дворецкий» is acceptable. Repeating a scenario phrase for every command is not.

## Executive conclusion

A single private Yandex Dialogs skill is the only currently demonstrated route that provides arbitrary recognized speech and a real multi-turn session to a custom backend. While the skill is active, every spoken turn arrives as `original_utterance`/`command`; returning `end_session:false` keeps the conversation open, so no per-command scenario is needed. ([Yandex request format](https://yandex.ru/dev/dialogs/alice/doc/ru/request), [SimpleUtterance](https://yandex.ru/dev/dialogs/alice/doc/ru/request-simpleutterance), [response format](https://yandex.ru/dev/dialogs/alice/doc/ru/response), accessed 2026-08-04; primary; confidence High.)

There is no supported or maintained local Glagol method that exports arbitrary microphone ASR text from an ordinary Alice session. AlexxIT/YandexStation can observe the command the station executed or inject text into Alice, but the maintainer explicitly distinguishes that from receiving what the user said. ([YandexStation README](https://github.com/AlexxIT/YandexStation/blob/master/README.md#L345-L360), [Glagol handler](https://github.com/AlexxIT/YandexStation/blob/master/custom_components/yandex_station/core/yandex_glagol.py#L112-L133), accessed 2026-08-04; primary; confidence High.)

The recommended topology is therefore hybrid at the speech boundary but local at the intelligence and home-control boundary:

`Alice speaker → Yandex ASR/private skill → public HTTPS relay → loopback-only gateway → local Ollama/Home Butler → bounded HA adapters → skill response → same speaker`.

## Technology landscape

### Private Yandex Dialogs skill

The skill protocol supplies `session_id`, increasing `message_id`, `session.new`, skill identity, and user/application state. The backend may keep its own history by `session_id` or round-trip `session_state`. Alice stays inside the skill until the backend returns `end_session:true`, the user exits, or the surface times out. ([Yandex request/session format](https://yandex.ru/dev/dialogs/alice/doc/ru/request), [activation and exit](https://yandex.ru/dev/dialogs/alice/doc/ru/activation), accessed 2026-08-04; primary; confidence High.)

A private skill is hidden from the public catalog and usable on surfaces logged into the publishing account, but it still must be published and pass automated checks. It is launched by name; only the separate smart-home skill class is activation-name-free. ([Yandex access management](https://yandex.ru/dev/dialogs/alice/doc/ru/access), [activation](https://yandex.ru/dev/dialogs/alice/doc/ru/activation), accessed 2026-08-04; primary; confidence High.)

The webhook must be publicly reachable over HTTPS with a valid certificate chain. A LAN address, localhost, or a self-signed certificate is not accepted. ([Yandex publication settings](https://yandex.ru/dev/dialogs/alice/doc/ru/publish-settings), [own-server deployment](https://yandex.ru/dev/dialogs/alice/doc/ru/deploy-server), accessed 2026-08-04; primary; confidence High.)

### YandexStation, scenarios, and Glagol

YandexStation's `yandex_scenario` route reports scenario metadata and the configured action, not arbitrary ASR. The well-known “phrase interception” gist differentiates a few pre-created scenarios through media side effects and does not form a general transcript channel. ([YandexStation README](https://github.com/AlexxIT/YandexStation/blob/master/README.md#L345-L360), [local-intent gist](https://gist.github.com/AlexxIT/d4995839aedde2bbcf822831a71a52c5#file-local_intent-md-L1-L42), accessed 2026-08-04; primary; confidence High.)

`ha-yandex-station-intents` improves finite scenario management but still starts from a predefined phrase list, documents a 200-scenario ceiling, and reports the canonical configured phrase rather than a free transcript. ([ha-yandex-station-intents](https://github.com/dext0r/ha-yandex-station-intents/blob/master/README.md#L62-L115), accessed 2026-08-04; primary; confidence Medium.)

`yandex-station-chat` and the YandexStation Conversation entity are text-injection paths: already available text is sent to Alice. They do not pass live microphone recognition to a local LLM. The proof-of-concept also disables TLS verification and asks users to hard-code/extract OAuth credentials, which is not acceptable for production. ([yandex-station-chat](https://github.com/gamahacka/yandex-station-chat/blob/main/README.md#L1-L34), [implementation](https://github.com/gamahacka/yandex-station-chat/blob/main/alice_chat.py#L164-L213), [YandexStation Conversation](https://github.com/AlexxIT/YandexStation/blob/master/custom_components/yandex_station/conversation.py#L47-L81), accessed 2026-08-04; primary; confidence High.)

### HTTPS relay choices

Tailscale Funnel can publish a local service through an account-owned `*.ts.net` HTTPS name and is available on all plans, but requires Tailscale installation and account approval. ([Tailscale Funnel](https://tailscale.com/kb/1223/funnel), accessed 2026-08-04; primary; confidence High.)

ngrok's free plan supplies one persistent development domain and its endpoints do not impose an application response timeout; the current machine already has ngrok 3.39.8, valid credentials, and the assigned domain `dancing-hull-numerous.ngrok-free.dev`. ([ngrok free limits](https://ngrok.com/docs/pricing-limits/free-plan-limits), [quickstart](https://ngrok.com/docs/guides/share-localhost/quickstart), accessed 2026-08-04; primary plus local inspection; confidence High.) This makes ngrok the lowest-friction current relay.

Cloudflare Tunnel is outbound-only and hides the origin, but a stable named public hostname normally requires a domain under Cloudflare DNS. ([Cloudflare connectivity options](https://developers.cloudflare.com/cloudflare-one/networks/connectivity-options/), accessed 2026-08-04; primary; confidence High.)

## Performance and benchmarks

Yandex requires the complete webhook response within 4.5 seconds, including connection setup, both network legs, backend processing, and the complete response body. Missing the deadline ends the skill session; streaming cannot extend a deadline measured until the complete response is received. ([Yandex response format](https://yandex.ru/dev/dialogs/alice/doc/ru/response), [publication settings](https://yandex.ru/dev/dialogs/alice/doc/ru/publish-settings), accessed 2026-08-04; primary; confidence High for the deadline, Medium for the streaming inference.)

Local measurements on 2026-08-04 showed:

| Path | Result |
| --- | --- |
| Existing general chat, 256-token cap | about 3.6 s warm; too close to 4.5 s |
| Cold GPU model load | about 15.3 s; impossible inside one Alice turn |
| Warm GPU, 64-token voice cap, 2048 context | 2.09–2.18 s for two representative turns |
| Deterministic local voice-status route | about 0.22 s |

The deployed `home-butler` model is Qwen 3.5 2.3B Q4_K_M. After an explicit warmup, Ollama reported 1,590,291,331 of 1,590,291,331 bytes in VRAM on the RX 6600 XT (`fully_on_gpu:true`). Keeping that runner resident for the voice service is therefore a correctness requirement, not only an optimization. (Local benchmark and `/api/ps`, 2026-08-04; primary local evidence; confidence High.)

## Comparative analysis

| Option | Arbitrary text | Multi-turn | Local-only | Main risk | Verdict |
| --- | --- | --- | --- | --- | --- |
| Private Yandex Dialogs skill + local model | Yes | Yes | No: ASR/dispatch use Yandex | 4.5 s and public HTTPS | Recommended |
| Yandex scenarios/intents | No, predefined phrases | No general session | No | Phrase explosion, 200-scenario ceiling | Rejected by requirement |
| Local Glagol interception | No demonstrated microphone transcript | No | Partly | Reverse-engineered firmware behavior | Not viable |
| Text injection into Alice | Input originates outside microphone | Limited/undocumented | Partly | Wrong direction, firmware-dependent output | Not viable |

## Implemented architecture

`scripts/alice_skill_gateway.py` now implements the local half of the recommended design:

- binds only to `127.0.0.1:8765`; Ollama and Home Assistant are never exposed;
- accepts POST JSON only on an unguessable webhook path;
- pins the Yandex `skill_id` and optionally owner `user_id` values;
- supports arbitrary multi-turn history by `session_id` with TTL and strict size limits;
- keeps `end_session:false` except explicit exit phrases;
- returns health-check `ping` without invoking the model;
- deduplicates repeated `message_id` values so a Yandex retry cannot repeat a HA action;
- rejects out-of-order turns and duplicate JSON keys;
- removes Markdown and caps speech below Yandex's 1024-character text/TTS limits;
- logs only route metadata and a one-way session fingerprint, never raw phrases or credentials;
- warms the model at startup, requires the trusted GPU endpoint and full VRAM residency, and uses a 64-token/2048-context voice profile;
- reuses Home Butler's existing deterministic HA read, incident, health, resource, and model-gated switch/light/button boundaries.

The loopback gateway and ngrok units are now enabled in a restricted provisioning mode. A random webhook path is stored only in a root-owned `0600` file. The first valid Yandex request may write exactly one private `0600` identity claim, but provisioning mode cannot call Ollama, Home Assistant, or any control path. A public round trip through ngrok was verified with a synthetic identity, and that test claim was then removed. The older four-scenario bridge is not the target architecture and should remain only as rollback evidence until the private skill passes a live speaker test.

## Security considerations

The public URL is a transport boundary, not an authorization boundary. The implementation combines an unguessable path, pinned skill identity, optional user allow-list, strict request schema, global rate limiting, bounded sessions, no raw utterance logging, and duplicate-message suppression. The ngrok relay forwards only to the loopback gateway. HA credentials remain systemd credentials and the model still cannot issue arbitrary service calls.

Private-skill visibility alone does not make the webhook network-private; the path secret must be rotated if exposed. The Home Assistant token pasted earlier in this project should also be rotated independently if it was ever exposed outside the protected setup flow.

## Risks and uncertainties

- The remaining hard blocker is one-time external setup: create the private skill, obtain its `skill_id`, enter the generated HTTPS webhook, and publish it.
- Real ngrok round-trip latency still needs measurement from Yandex's console and a physical Station. The local 2.1-second budget leaves roughly 2.4 seconds for Yandex and relay overhead, but this is not yet end-to-end proof.
- Long HA proof/control paths may exceed 4.5 seconds even when ordinary model conversation fits. They fail closed, but live measurements are required.
- Glagol has no official public schema; an undocumented firmware-specific ASR event cannot be ruled out absolutely. No maintained project reviewed demonstrates it, and the leading integration says the phrase is unavailable. ([YandexStation README](https://github.com/AlexxIT/YandexStation/blob/master/README.md#L345-L360), accessed 2026-08-04; primary; confidence High.)

## Next steps

1. Create a private Yandex Dialogs skill under the same account as the Station.
2. Copy the prepared HTTPS URL from the local root-only file into the skill's Webhook field and run the console check; no ID or token needs to be copied into chat.
3. Finalize the captured first-request identity, restart into the GPU-backed full-dialog mode, and verify the pinned skill/optional user allow-list.
4. Publish privately and run three live proofs: two unrelated conversational turns, a follow-up that uses session history, and one exact HA read/control with readback.
5. Disable the obsolete scenario bridge only after the full-dialog path is proven on the speaker.
