# Phase 66 — матрица обязательной приёмки A–Z

Дата проверки source/runtime: 2026-08-25. `PASS offline` означает автоматический тест
working tree. `PENDING live` означает, что опасный/изменяющий live-тест не
выполнялся без отдельного разрешения владельца.

| ID | Статус | Доказательство |
|---|---|---|
| A | PASS offline | полный `unittest discover`: 693 tests, OK, 1 skipped; safety tests не удалялись |
| B | PASS offline | `test_production_call_sites_have_no_manual_context_or_model_route` |
| C | PASS read-only live | отдельный dialogue-profile turn завершён; `/api/ps`: `qwen3.5:4b-q4_K_M`, 4.7B Q4_K_M, `context_length=32768`, `size_vram=size`, full VRAM. После voice/structured запроса ожидаемо показывается 8192 |
| D | PASS offline | `test_100_turns_restart_and_cross_transport_keep_alias_goal_and_preference` |
| E | PASS offline | тот же restart/cross-transport test и повторное открытие SQLite |
| F | PASS offline | bounded-agent coreference read/action tests для одного physical device |
| G | PASS offline | vacuum fixture с 26 features сообщает только diagnostic component |
| H | PASS offline | неизвестный dishwasher consumable, без literal production rule |
| I | PASS offline | неизвестный numeric code без выдуманной расшифровки |
| J | PASS offline | partial entity + available sibling/alternate integration не дают device recovery |
| K | PASS offline | transient unavailable закрывается до confirmation/debounce; повторный outage после уже озвученного recovery создаёт новый device-episode и не наследует старый dedup |
| L | PASS offline | private IP-drift journal + exact stable-identity recovery diagnosis |
| M | PASS offline | definite failure переходит ко второму declared safe step; verified останавливает ladder; delivery-unknown также останавливает |
| N | PASS offline | scheduler, automation, reminder и recovery tests запрещают retry после delivery-unknown |
| O | PASS offline / PENDING live | Funnel reassert/restart budget source ≤ 41.2 s; controlled real outage не выполнялся |
| P | PASS offline / PENDING live | gateway/Funnel/Tailscale/model/HA isolation и circuit breaker; реальные component-stop tests не выполнялись |
| Q | PASS offline | natural reminder без слова «чтобы» создаёт persistent TaskSpec |
| R | PASS offline | daily report reschedule меняет единственный scheduler source и test-clock run |
| S | PASS offline + read-only live | restart выполняет one-shot ровно один раз; production scheduler timer/oneshot перезапущены только при `not_due`, task `system-daily-report`, next run и attempts сохранились, `executed=0` |
| T | PASS offline | natural find/update/cancel без duplicate execution |
| U | PASS offline | исправленный alias supersede-ит старый и retrieval возвращает новый |
| V | PASS offline + read-only live | onboarding schema-1→2 migration, partial answers, proposal, только неизвестные вопросы, exact deterministic approval без model-facing opaque ID/hash; evaluator 7/7 и production local-chat read; до approval и после него нет HA write (`actions_performed=0`) |
| W | PASS offline | hostile entity/attribute/log/memory data не повышают intent и не вызывают action |
| X | PASS offline | proposal → isolated candidate → qualification → exact approval; без deploy; failed health rollback |
| Y | PASS offline | compact discovery/details/history tools; full entity snapshot не доступен обычному agent loop |
| Z | PASS offline + read-only live | 30 полных warm Alice turns: P95 2.981 s, 30/30 model-completed, 0 fallback при budget 3.2 s; slow turn создаёт durable task; restart не повторяет action |

Дополнительная runtime-проверка фоновой нагрузки: startup self-check теперь
one-shot после запуска (`OnBootSec=90s`, без `OnUnitActiveSec`), фактический
запуск завершён `ready`, после чего timer имеет `NEXT=n/a`. Периодический
heartbeat остаётся отдельным timer каждые 10 минут. Scheduler regression также
доказывает, что literal `13:00` существует только в seed-записи TaskSpec, а не
в systemd/supervisor/chat/wake helper. Полная регрессия после этих изменений:
693 tests, OK, 1 skipped; evaluator 7/7 и no-cloud PASS.

Deployment parity проверен отдельно: 68/68 runtime scripts и 58/58 managed
units совпадают с source; преобразуемые Hermes/HA config и runtime policies
совпадают после документированной normalization. Старые `*.before-*` копии и
неуправляемый alias перенесены в root-only backup. Новый installer guard
запрещает любые дополнительные файлы рядом с production scripts. После
изменения: 693 tests, OK, 1 skipped; evaluator 7/7, no-cloud и runtime policy
PASS.

GPU runtime также source/runtime-qualified: приватный `flock`
запрещает второй WSL supervisor. Live readback показал ровно один
процес, lock owner `homebutler`, mode `0600`; dialogue-turn загрузил
Qwen 4.7B с `context_length=32768` и full VRAM. При удалении дубликатов
Windows task закрыла дочерний Ollama; supervisor автоматически
восстановил его как PID 70996. HA, устройства, Tailscale и Linux
`ollama.service` не перезапускались.

## Что остаётся live-квалифицировать

- O/P требуют отдельного разрешения на кратковременную остановку exact
  Funnel/tunnel или Alice skill service. Home Assistant и устройства для них
  перезапускать не нужно; controlled O/P по указанию владельца не выполнялся.
- Реальная доставка local reminder и ежедневного отчёта через колонку после
  scheduler migration не выполнялась только ради теста.
- Динамический Windows one-shot wake уже source/runtime-qualified: старый
  ежедневный trigger удалён, nearest `wake_epoch` синхронизирован, readback
  `WakeToRun=true`. Физическое wake-from-sleep остаётся ненаблюдавшимся live
  событием и не считается доказанным.
