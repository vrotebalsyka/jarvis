# Phase 66 — фактический baseline перед эволюционным апгрейдом

Дата аудита: 2026-08-23. Все проверки в этом отчёте выполнялись без
переключения устройств, без recovery Home Assistant, без обновлений и без
раскрытия credentials, IP/MAC или приватных идентификаторов.

## 1. Источники истины

Порядок проверки:

1. локальный Git index и рабочее дерево;
2. развёрнутый `/opt/home-butler`;
3. установленные systemd units/timers и их фактическое состояние;
4. read-only runtime probes;
5. текущий вывод тестов;
6. документация.

Фактический Git root: `/root/Jarvis/home-butler`.

Исходная контрольная точка:

- branch: `main`;
- HEAD: `4d45d9c721955cce6f3ed2a7e7fdcb927ce5019a`;
- сообщение: `Home Butler: initial public import`;
- рабочее дерево до Phase 0 было чистым;
- вложенный `hermes-agent/.git` существует отдельно, но не является корнем
  Home Butler.

Phase 0 не выполнял commit, push, reset или переписывание history.

## 2. Manifest и классификация репозитория

Добавлен secret-safe генератор `scripts/repository_manifest.py` и файл
`reports/PHASE-66-REPOSITORY-MANIFEST.tsv`. Manifest содержит путь, категории,
размер, тип и SHA-256, но не выводит содержимое файлов.

Начальный manifest видимого дерева после hygiene-миграции:

| Показатель | Значение |
|---|---:|
| Файлов | 237 |
| Суммарный размер | 2 407 286 байт |
| Source | 74 |
| Tests | 64 |
| systemd/config | 72 |
| Documentation | 26 |
| Binary assets | 1 |

Структурный аудит source/tests/config/docs охватил 51 505 строк. Python source
и tests разобраны через AST (imports, classes, functions); shell, PowerShell,
systemd, model config и документация классифицированы отдельно. Binary/cache/DB
не интерпретировались как текст.

## 3. Tracked runtime artifacts

В исходном HEAD Git отслеживал 22 runtime-generated артефакта Hermes:

- history и lock/check files;
- три cache JSON;
- три log files;
- model/context discovery caches;
- hub/usage state;
- `state.db`, `state.db-shm`, `state.db-wal`.

Миграция Phase 0:

- файлы сохранены в работающем runtime;
- они удалены только из Git index (`git rm --cached`);
- `.gitignore` дополнен точными runtime paths;
- добавлен `tests/test_repository_hygiene.py`;
- тест запрещает повторное попадание DB/WAL/log/cache/history в Git;
- публичная история не переписывалась: старый commit всё ещё содержит эти
  артефакты; её очистка возможна только по отдельному разрешению владельца.

Secret-safe scan текущего tracked content не обнаружил JWT, Bearer token или
private key block. Эвристические совпадения по credential-like именам оказались
именами cookie/test fixtures; значения и приватные данные в отчёт не выводились.

## 4. Baseline тестов до функциональных изменений

| Команда | Фактический результат |
|---|---|
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | 501 tests OK, 1 skipped, 79.711 s |
| `python3 tests/evaluate_model.py` | процесс завершился с code 0, но `all_pass=false`; 4 из 5 checks прошли |
| `./scripts/no_cloud_audit.py` | PASS; cloud credentials/fallback отсутствуют, используется локальная модель |
| `python3 -m unittest tests.test_repository_hygiene -v` | 3 tests OK |

Непрошедший model check — `Safe tool selection`. Его нельзя маскировать
успешным exit code; это входной defect для Phase 1/7.

Финальная проверка после Phase 0 migration:

| Проверка | Результат |
|---|---|
| Полный offline suite | 504 tests OK, 1 skipped, 84.265 s |
| Повторный model evaluation | 4/5 checks; тот же `Safe tool selection` остаётся красным |
| Повторный no-cloud audit | PASS |
| Manifest consistency | 238 expected, 238 actual, 0 missing/extra/changed |
| `git diff --check` | PASS |

Таким образом, hygiene-миграция не внесла regression. Model evaluation
намеренно не объявлен зелёным: известный defect должен исправляться через
единую runtime/tool policy, а не подгонкой итогового отчёта.

## 5. Runtime snapshot

### 5.1. Systemd и deployment

- 56 systemd-файлов репозитория установлены в `/etc/systemd/system`;
- hashes всех 56 установленных unit files совпадают с Git working tree;
- установлен дополнительный runtime unit для Windows GPU supervisor;
- drop-ins для Home Butler не обнаружены;
- Alice skill, tunnel, incident monitor и local chat активны;
- `home-butler-dialogue-qualification.service` находится в failed state;
- recovery timers для automation, integration, Core, out-of-band и общего
  recovery выключены;
- `home-butler-voice-intent.service` выключен и неактивен.

Сравнение Git и `/opt/home-butler`:

| Результат | Количество |
|---|---:|
| Сравнено | 77 |
| Совпадает | 55 |
| Отличается | 3 |
| Отсутствует в deployment | 19 |

Отличаются deployed `AGENTS.md`, `hermes/SOUL.md`, `hermes/config.yaml`.
Последний файл может содержать ожидаемую runtime-настройку, но расхождение всё
равно должно стать управляемой конфигурацией. В deployment также лежат семь
backup-копий вида `.before-*`.

### 5.2. Модель и ускоритель

Ollama runtime:

- version: `0.32.5`;
- доступны локальные 2B и 4B Qwen 3.5 модели и два Home Butler alias;
- активный production alias: `home-butler:latest`, 2.3B, Q4_K_M;
- при probe модель целиком находилась в VRAM;
- активный context на одном runtime probe: 2048;
- model evaluation отдельно загрузил context 4096.

Это доказывает, что GPU offload работает, но не доказывает заявленный 64K
production context. Фактический context зависит от route и ручного hardcode.

### 5.3. Home Assistant

- read-only `/api/config` отвечает;
- Core version: `2026.7.4`;
- state: RUNNING;
- safe mode: false;
- API не вернул надёжный `installation_type`;
- существующий проверенный deployment/recovery topology использует официальный
  Home Assistant Container/Core container, а не HAOS/Supervisor.

`/api/system_health/info` через текущий ограниченный adapter не был доступен.
Это зафиксировано как отсутствие evidence, а не как неисправность HA.

### 5.4. Alice и local chat

- local gateway активен;
- public Funnel probe на момент аудита успешен;
- guardian сохраняет состояние и выполняет bounded exact-unit restart;
- dialogue qualification state не согласован с живым probe: qualification
  service failed/stale;
- local browser chat доступен на localhost и через отдельный LAN backend;
- LAN path защищён owner key, сессией, CSRF, Host/Origin checks и firewall,
  ограниченным домашней подсетью.

Public ping не вызывает полноценный model turn, поэтому сам по себе не
доказывает готовность свободного разговора.

### 5.5. Scheduler и отчёт

Единого persistent scheduler пока нет.

- ежедневный отчёт управляется systemd timer;
- timer содержит четыре жёстких слота: 13:00, 13:05, 13:10, 13:15;
- `Persistent=true`;
- `operations_supervisor.py` также содержит hardcoded hour 13 и 15-минутный
  retry grace;
- последний status record имеет verified delivery и один service call;
- reminder adapter умеет одну проверяемую отправку на Station, но не является
  полным persistent CRUD scheduler.

### 5.6. Persistent state

Bounded model workspace уже существует и должен быть сохранён:

- quota: 8 GiB;
- mode root directory: 0700;
- допустимы только текстовые форматы и пять верхнеуровневых папок;
- executable content и изменение active project instructions запрещены;
- на момент аудита: 5 файлов, 282 021 байт;
- workspace — untrusted reference storage, а не диалоговая память.

Incident store уже является значимой системой и не должен дублироваться:

- SQLite WAL;
- 30 domain tables;
- 4 392 rows суммарно;
- имеются entity/device mapping, physical-device incidents, network and
  integration observations, notifications, service-call observations,
  recovery decisions/actions и evidence JSON.

Device knowledge catalog versioned и содержит 39 физических устройств на
момент snapshot. В отчёте намеренно не перечислены имена, IDs, IP или MAC.

## 6. Документация против фактического runtime

| Claim | Source | Runtime evidence | Actual status | Required correction |
|---|---|---|---|---|
| Корень может быть `/root/Jarvis` | старые инструкции | `git rev-parse --show-toplevel` | корень `/root/Jarvis/home-butler` | использовать только обнаруженный Git root |
| Production context 64K | README, preflight, continuation | Modelfile/config/calls и `/api/ps` | routes используют 1536/2048/3072/4096/8192 | Phase 1: единая Runtime Policy + benchmark |
| 64K полностью в VRAM | README/preflight | текущий `/api/ps` | full GPU доказан только для текущего меньшего context | не заявлять 64K до benchmark evidence |
| Управление только switch/light/button | README/старые sections | control map и tests | также fan, humidifier, siren, vacuum, number, select | описать capability catalog, не терять текущие функции |
| Alice проверяется каждые 30 секунд | PROJECT-CONTINUATION | installed timer | фактически 60 секунд после boot delay | Phase 5: измеренная cadence |
| Skill/Funnel восстановлены примерно за 22 секунды | PROJECT-CONTINUATION | guardian threshold/waits | 3 failures, 60-s cadence и 90-s wait не дают такой гарантии | измерить P95 и исправить guardian |
| Recovery live | отдельные docs/runtime services | `is-enabled/is-active` | основные recovery timers disabled | не называть staged recovery включённым |
| Система HAOS | README/UI wording | container upgrade topology | Home Assistant Container/Core | заменить HAOS на Home Assistant/Container там, где речь о deployment |
| Отчёт ровно в 13:00 | README/unit description | installed timer | четыре retry slots до 13:15 | Phase 6: единый scheduler и честный status |
| Workspace даёт постоянную память | UI/старые ответы | workspace schema/status | bounded файлы без retrieval/session memory | Phase 2: отдельный SQLite Memory Store |
| Public ping означает готовность навыка | operational assumption | ping и dialogue qualification | ping может обходить model turn; qualification failed/stale | Phase 5: component health + synthetic model probe |
| Documentation и deployment едины | README/continuation | hash comparison | три ключевых deployed files отличаются | определить source of truth и управляемый deploy |

## 7. Root causes, подтверждённые Phase 0

1. Model runtime policy размножена по production modules; заявленная модель и
   context не совпадают с отдельными routes.
2. Conversation backend монолитен и совмещает routing, prompts, tool selection,
   validation и formatting.
3. Workspace используется как файлы, но нет индексируемой owner memory,
   conversation summaries и durable active goals.
4. Device/incident foundations уже сильные; главная недостающая часть —
   безопасная semantic feature model и on-demand retrieval, а не новый graph.
5. Alice health смешивает transport availability и фактическую готовность
   model turn; qualification evidence может устаревать.
6. Report/reminder scheduling распределён между timer, supervisor и transport.
7. Runtime/config drift не имеет одного проверяемого deployment contract.
8. Публичный Git импорт ошибочно включил runtime DB/WAL/log/cache/history.

## 8. Phase 0 safety conclusion

На Phase 0 не разрешено и не выполнялось:

- recovery или restart Home Assistant;
- переключение устройств;
- live stop/restart qualification Alice;
- обновление HA, integrations, Ollama или моделей;
- загрузка пакетов/моделей;
- Git push/history rewrite.

Следующий допустимый этап после финального regression run — Phase 1:
`ModelRuntimePolicy`, runtime tracing и локальный benchmark уже установленных
2B/4B моделей. Ни новый Memory Store, ни новый DeviceGraph до этого не создаются.
