# Jarvis — выполненная работа и проверка скриншотов, 9 сентября 2026

Дополнение 10 сентября: проект и отчёты сохранены в GitHub Stage73 branch,
после чего по разрешению владельца Ollama/Jarvis остановлены: 12 units masked,
5 Windows tasks Disabled, Ollama/llama процессов нет. Исходники, веса модели
и закрытые локальные secrets не удалены; secrets/weights в Git не публикуются.
Подробности и инструкции продолжения: [CURRENT-GOAL](../CURRENT-GOAL.md).

Последнее дополнение — owner-approved nullable registry contract, code commit
`5b359032ab80c97b3417a6b01cfca16684ca63bb`. `registry_area=null` теперь хранится
отдельно от owner room; это не wildcard и не разрешение менять HA registry.
Repository 159/159, Stage71 PASS, Stage72 natural 100/100, room/type 42/42.
Четыре выбранные однозначные цели дают sealed canary plans в OFF-only diagnostic;
реле остаётся clarification. Старый frozen Stage73 shadow по-прежнему 170/205.
Phase A не green, Phase B не разрешена, реальные HA_POST/service calls=0.
Свежие числа и ограничения — в начале [STAGE-73-RESULT](../STAGE-73-RESULT.md).

Предыдущее дополнение (до nullable contract):

Дополнение после публикации: HA восстановил доступность без наших изменений.
Свежий повтор: Stage71 PASS, Stage72 natural **99/100** (N04 timeout), room/type
**42/42**, Stage73 shadow **170/205**, owner-review **24/25**. HA_POST=SERVICE_CALLS=0.
Подробности и ссылки на новые traces находятся в начале
[STAGE-73-RESULT](../STAGE-73-RESULT.md). Ниже сохранён отчёт checkpoint `284b99a`,
в том числе состояние HA и публикации на момент первоначальной подготовки.

## Итог

Stage 73 **не завершён**: Phase A `NOT_READY`, Phase B
`INCOMPLETE_LIVE_APPROVAL_REQUIRED`. Реального управления и испытаний устройств
не было. Stage 74 не начат. Публикация промежуточной работы отдельно запрошена
владельцем; она не означает прохождения gates, promotion или разрешения на live.

Рабочая ветка: `stage73-canary-live-control`. Base и последний проверенный main:
`8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`.
Точный снимок публикуемой работы определяется commit, содержащим этот отчёт;
он будет отдельно назван владельцу после создания. GitHub main не изменяется.

В production остался Stage 72 SHADOW с двумя отдельно разрешёнными hotfix:
CSRF веб-чата и Unicode-проверка ответа. Это **не полное содержимое ветки Stage73**
и уже не byte-identical main. Canary adapter, его конфигурация и action credential
в production не установлены. Local chat перезапускался только для этих hotfix;
Alice, HA и сетевые настройки не перезапускались/не изменялись.

## Что выполнено

1. Подготовлена Phase A: owner-managed allowlist, оба control flags default OFF,
   immutable sealed plan с TTL, fresh identity/area/domain revalidation,
   один ограниченный light/switch on/off adapter, durable idempotency,
   независимые before/after/stability reads и sticky emergency OFF.
   Это код для дальнейшей приёмки, **не подтверждение готовности к live**.
2. Исправлены общие ошибки weak-evidence/action semantics: перестановка букв,
   проверка полного physical name, сохранение конкретного entity channel,
   естественные формулировки desired state и room-qualified ambiguity.
   Домашние имена не добавлялись как специальные production rules.
3. Подготовлены live runner и GET-only result events для
   `accepted_unverified`/`delivery_unknown`. Runner проверялся только с fake HA;
   live CLI не запускался, owner approval не установлен.
4. Созданы независимый oracle, frozen shadow corpus и owner-review draft
   на 25 фраз для пяти выбранных владельцем устройств. Draft не объявлен
   owner-reviewed или live-authorized; ожидания после ответов не переписаны.
5. Исправлен CSRF-баг: JavaScript чата и входа выбирал первый `<meta charset>`,
   а не именованный CSRF meta. В production перенесены ровно две замены selector.
   Проверка токена и проверка ключа владельца сохранены.
6. Исправлен отказ на безопасные emoji/Unicode в ответе модели. Текст и числа
   сохраняются; прежние secret/technical-ID regex проверяют исходную строку и
   дополнительную Unicode-normalized копию. Добавлены проверки маскировки,
   невалидных суррогатов и управляющих символов. Модель не менялась.
7. Диагностирована недоступность HA. TCP timeout повторился до HTTP/auth/registry,
   хотя настроенный адрес отвечал на ICMP. Listener, фильтрация порта и identity
   отвечающего узла пока не различены. Точная причина на стороне HAOS не доказана.

История и подробности: [STAGE-73-RESULT](../STAGE-73-RESULT.md),
[CSRF evidence](local-chat-csrf-hotfix-2026-09-09.json),
[Unicode evidence](local-chat-unicode-hotfix-2026-09-09.json).

## Разбор четырёх новых скриншотов владельца

Источник — изображения, присланные в этой переписке 9 сентября. Это наблюдение
owner-facing UI, а не новые instrumented tests. Кадры перекрываются, поэтому
одни и те же реплики не посчитаны повторными испытаниями. Исходные PNG по
переданным локальным путям не обнаружены и в пакет файлов не включены;
ниже сохранён разбор видимого текста. Технические IDs не публикуются.

| Фраза | Что видно в ответе | Вывод и ограничение |
| --- | --- | --- |
| «включи свет в кабинете» | «Shadow-план построен: включить Свет. Ничего не отправлено в Home Assistant.» На первом кадре ответ повторяется. | Планирование работает для этой формулировки. UI не доказывает private physical identity, свежую registry area или реальное включение. Повторный shadow-план не доказывает idempotency будущего live adapter. |
| «включи вытяжку на кухне» | Shadow-план на human label «Вытяжка на кухне», явно сказано, что в HA ничего не отправлено. | Полное имя найдено в metadata. Это не fresh read, не подтверждение питания и не live receipt. |
| «давай включим реле вентилятора» | «Уточните цель: реле вентилятора; реле вентилятора.» | Нет угадывания между кандидатами, но clarification непригодно для выбора: оба видимых варианта одинаковы. |
| «реле вентилятора» | Повтор того же уточнения с двумя одинаковыми названиями. | Владелец попадает в цикл без различимых вариантов. По скриншоту нельзя утверждать, что выбрано нужное tuya_local устройство. |
| «включи реле вентилятора в кабинете» | «Найдены равные кандидаты. Уточните цель; shadow-план не создан.» | Комната не сняла ambiguity. План не создан; обычная желаемая команда пока не обслуживается. Причину именно этого turn нужно подтверждать trace/fresh metadata. |
| «включи вытяжку» | Равные кандидаты; shadow-план не создан. | Короткое имя неоднозначно. Это совместимо с ранее найденными разными physical targets, но скриншот не заменяет fresh registry read. |
| «включи ночник в кабинете» | Равные кандидаты; shadow-план не создан. | Команда не обслуживается. Возможные связи с отсутствующим registry binding и несколькими выходами известны по прежнему graph, но текущая причина без свежего чтения не доказана. |

Всего **7 уникальных формулировок: 2 с показанным plan, 5 с clarification**.
Это счёт видимых исходов, **не 7-case acceptance PASS**. Нельзя вычислить
WRONG_TARGET, физические side effects, latency или HA_POST только по картинкам.
На них нет заявления «физически включено»: ответы явно помечены shadow.

### Что нужно исправить/проверить дальше — без разрешения на выполнение сейчас

- Сделать варианты clarification различимыми безопасными признаками из metadata:
  integration, verified registry area и конкретный выход, если эти признаки
  действительно различаются. Не показывать private IDs, не подставлять
  owner/inferred room как registry fact и не выбирать по allowlist вместо resolver.
- Если различимых подтверждённых признаков нет, честно сообщать это, а не
  предлагать выбрать между двумя одинаковыми надписями. Не объединять physical
  targets из-за одинакового имени и не угадывать ответ.
- Восстановить доступность HA и получить свежие registry/states. Владелец
  запретил менять registry rooms только ради зелёных тестов; запрет сохраняется.
- После восстановления повторить live read/oracle и shadow acceptance.
  Любое реальное управление требует отдельного Phase B approval.

## Доказанные проверки и оставшиеся gates

| Проверка | Последний зафиксированный результат | Ограничение |
| --- | --- | --- |
| Repository unittest после Unicode-hotfix | **145/145 PASS**, 55.977 s | Выполнено до подготовки этого отчёта; это не live acceptance. |
| Repository contract после добавления отчёта | **6/6 PASS**, 0.131 s | Новый Markdown добавлен точечно в closed set; полная Linux suite в этом report-only шаге заново не запускалась. |
| Staged Stage72 runtime compatibility | **14/14 PASS**, 0.061 s | Unicode, security и CSRF; fake ответы/endpoint в изолированных тестах. |
| Browser → production → реальная модель | «привет» HTTP 200; «спасибо» HTTP 200 с 🤗 | 1481/2182 ms; обычные разговорные ответы, не HA facts. |
| Просьба «Поздоровайся и добавь эмодзи…» | HTTP 200, но clarification вместо conversation | Незакрытый parser limitation; не засчитан semantic PASS. |
| Stage71 fresh live/oracle | **BLOCKED / InventoryError** | HA недоступен; старый PASS не выдаётся за текущий. |
| Stage72 natural | **100/100**, P50/P95/P99 0.0200/0.1500/2.2342 s | Ранее выполненный frozen fixture corpus, не новый прогон текущего дома. |
| Stage72 room/type snapshot | **42/42**, 21 room/type plans; 1.5256/1.6728/1.7204 s | Snapshot, не fresh HA acceptance. |
| Stage73 frozen shadow snapshot | **170/205**, 183 уникальные фразы; 1.5402/2.1397/2.4466 s | 35 несовпадений старых expected plans из-за relay ambiguity; ожидания не изменены. |
| Новый owner-review draft | **24/25** совпадений; 17 plans, 8 clarifications | Owner-reviewed=0; R20 остался расхождением expected plan против безопасного clarification. |
| Phase B / реальные циклы | **0 устройств испытано, 0 циклов** | Не разрешена и не запускалась. |

В последнем instrumented Stage73 snapshot: WRONG_TARGET=0, CROSS_ROOM_TARGET=0,
AMBIGUOUS_PLAN=0, FALSE_ACTION_INTENT=0, FORBIDDEN_PLAN=0, WRONG_ACTION=0,
MISSED_EXPECTED_PLAN=35; HA_GET=REGISTRY_READS=HA_POST=SERVICE_CALLS=0.
Это **метрики того snapshot**, не измерение недоступного сейчас HA и не
доказательство выполненных физических команд со скриншотов.

Последняя диагностика 9 сентября: WSL TCP timeout 3.0043 s; Windows TCP timeout
3.1325 s; Windows ICMP success 68 ms. В этих probes application requests=0.
Токен не участвует в неудачном TCP handshake; менять его ради этого timeout
оснований нет. HAOS-консоль пока не предоставлена, место установки HAOS не уточнено.

## Код в рабочей ветке и реально установленный код — не одно и то же

| Изменённый production-source файл в ветке | Строк до → после |
| --- | --- |
| `scripts/canary_contract.py` | 0 → 294 |
| `scripts/canary_verifier.py` | 0 → 111 |
| `scripts/canary_write_adapter.py` | 0 → 340 |
| `scripts/bounded_ha_agent.py` | 1137 → 1294 |
| `scripts/home_assistant_mcp.py` | 1084 → 1146 |
| `scripts/shadow_action_policy.py` | 175 → 188 |
| `scripts/owner_chat.py` | 148 → 153 |
| `scripts/local_chat_gateway.py` | 281 → 314 |
| `scripts/alice_skill_gateway.py` | 751 → 754 |
| `scripts/install-home-butler-service.sh` | 135 → 138 |
| Все `scripts/*.py` и `scripts/*.sh` | **6347 → 7368** |

Production-source файлы не удалялись. Единственный conversational path,
resolver, HomeGraph и ActionPolicyRegistry сохранены. Увеличение числа строк
отражает подготовленный canary safety/verification код, а не развёрнутый executor.

В runtime установлены только Stage72 версии с hotfix:

- `local_chat_gateway.py`: 281 → 281 строк; SHA256
  `c0f4b80dd4e81c8a299294aa1c78593292dd0c3d67f7f003404a1af325f9018e`.
- `bounded_ha_agent.py`: 1137 → 1143 строки; SHA256
  `58126c55deb6b035480e298bf5fc35cfe8dd5b3bfce08668a9e69f96365f5084`.

Alice не перезапускалась: новая функция в общем файле на диске не означает,
что её уже использует текущий процесс Alice. Полная Alice acceptance не заявляется.
Модель не менялась: qwen3.5:2b-q4_K_M. Ранее зафиксированный digest:
`124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`;
context 8192, historical size_vram=0. Количество фактически offloaded layers
не измерено; GPU offload не объявляется доказанным. Нового замера модели в ходе
подготовки этого отчёта не было.

## Пакет evidence и публикация

Сохраняются и успешные, и неуспешные промежуточные результаты:

- [Развёрнутый Stage73 report](../STAGE-73-RESULT.md).
- [Owner-review таблица, 25 фраз](STAGE73-OWNER-REVIEW.html).
- [Последний Stage73 snapshot, 170/205](stage73-shadow-final-snapshot-2026-09-08.json).
- [Последний room/type snapshot, 42/42](stage73-stage72-room-type-final-snapshot-2026-09-08.json).
- [Owner-review snapshot, 24/25](stage73-owner-review-final-snapshot-2026-09-08.json).
- [Диагностика HA](stage73-ha-connectivity-2026-09-08.json).
- [CSRF hotfix](local-chat-csrf-hotfix-2026-09-09.json) и
  [Unicode hotfix](local-chat-unicode-hotfix-2026-09-09.json).
- Остальные `stage73-*.json` в этом каталоге — неизменённая история replay,
  ошибок, исправлений и повторных проверок. Они не заменены одним «зелёным» итогом.

На момент подготовки отчёта GitHub-публикация **заблокирована**: автоматическая
проверка разрешений отклонила даже read-only `git ls-remote` с сообщением
`workspace is out of credits`. Это не отказ GitHub в авторизации и не доказанная
проблема remote. Сетевой запрет не обходился. Успешный push и remote SHA
**не подтверждены**; нельзя считать этот отчёт уже загруженным на GitHub.

Последующее подтверждение публикации 9 сентября: blocker исчез, выполнен обычный
fast-forward push в `stage73-canary-live-control`. Независимый `git ls-remote`
подтвердил `284b99a4619ec4153cb0b7c5a4381b07cec1b7bc`; main остался на
`8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`. Выше сохранено состояние именно
на момент подготовки, не текущее состояние GitHub. Production не обновлялся.

Реальные allowlist IDs, токены, текущие HA states и исходный private inventory
в пакет не включаются. Четыре скриншота разобраны выше по изображениям в сообщении;
их отсутствующие на диске PNG не подменены реконструкцией.

Проверка подготовленного пакета: 54 файла, из них 22 отчётных материала;
22 JSON/JSONL успешно разобраны, ссылки нового отчёта существуют, оба Stage73
frozen manifests сохранили исходные SHA256. Паттерн-проверка отчётов на токены,
private IPv4 и HA entity IDs совпадений не нашла. Это ограниченная проверка
публикуемых материалов, не универсальная гарантия отсутствия всех видов секретов.

Статус работы: **промежуточный отчёт, Stage73 NOT_READY; live approval отсутствует**.
