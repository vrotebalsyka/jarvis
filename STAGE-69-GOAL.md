# /goal — Stage 69: сначала достоверное ядро, потом обучение

Работай с `main` репозитория `vrotebalsyka/jarvis` после Stage 68.

## Главная цель

Не добавляй vision, новые recovery playbooks, новые модели, self-patching или новые бытовые функции. Сначала закрой провалы живой пользовательской приёмки Stage 68.

Jarvis должен:

1. Перед ответом о доме брать **текущее** состояние из Home Assistant. Learned profile хранит семантику, но никогда не является источником current state/value/availability.
2. Читать любое существующее физическое устройство, даже если оно ещё не проходило learning pipeline.
3. Находить устройство по обычной речи: HA display name, friendly/original name, alias, area/room, типу устройства и их комбинациям.
4. Не требовать `entity_id`, английское имя или техническое полное имя, если цель можно определить из HA. Примеры класса: `Андрей`, `роботом Андреем`, `Roborock`, `посудомойка` -> HA `Dishwasher`, `свет кабинет`, `свет туалет`, `свет прихожка`.
5. При нескольких равноправных целях задавать один короткий вопрос и **не выполнять** действие.
6. Никогда не придумывать и не отрицать причину без evidence. Если HA не доказал Wi-Fi/сеть/интеграцию/модуль: `причина по текущим данным не подтверждена`.
7. Считать действие успешным только при `verified`. `accepted`, `accepted_unverified`, `partially_verified`, `delivery_unknown` и HTTP 200 не дают права говорить `включил`, `выключил`, `запустил`, `готово`.
8. Для device-level `включи/выключи` выбирать единственный primary power/main control, если он однозначен. Не выбирать `Storage`, `Половинная загрузка` или вторичную функцию вместо питания.
9. Проверять switch/light/fan/select/number достаточно долго, чтобы transient `off -> on -> off` не считался успехом.
10. Не делать вывод `готово к работе` из одного `power=on`. Смысл запуска прибора подтверждается отдельным status/progress/time transition.

## Обязательные исправления

### A. Browser chat

Убери автоматический префикс `/модель` из обычного веб-чата. Сейчас галочка `Свободный ИИ` по умолчанию отправляет `/модель ...` и обходит `answer_natural`/bounded HA agent. Обычный браузерный вопрос должен идти тем же grounded HA path, что и Alice. Явный `/модель` оставь только как ручную команду для не-HA свободного разговора.

### B. Universal live read

Убери gate, по которому fast read разрешён только устройствам с `DeviceKnowledgeProfile`. Если physical device найден однозначно, всегда читай его live details. Если learned profile есть — используй его только как semantic overlay. Если нет — используй registry metadata + live states и generic grounded renderer.

### C. Один resolver поверх существующего DeviceGraph

Не создавай второй DeviceGraph. Resolver должен ранжировать:

- physical-device display name;
- entity friendly/original names;
- device/entity aliases;
- area name + area aliases;
- manufacturer/model;
- HA domain/semantic type;
- integration metadata только как слабый сигнал.

Добавь generic русский morphology/token normalization и type concepts. Типовое слово не должно ломать конкретное имя/комнату: `свет кабинет` = room/name `кабинет` + light concept. `посудомойка/посудомоечная/дисвашер` должны находить `Dishwasher` без хардкода конкретного entity_id.

### D. Action receipts

Исправь все уровни агрегации: plan `verified` только если **каждый** выполненный шаг `verified`. Любой accepted/unverified делает весь plan не verified. После `delivery_unknown` не повторять автоматически.

### E. Grounded owner response

После HA tool result ответ должен проходить deterministic validation/rendering. Запрещено:

- терять актуальное live значение;
- превращать available feature в unavailable;
- использовать старое значение из learning corpus как current state;
- утверждать или отрицать неизвестную причину;
- объявлять физический успех без verified receipt;
- показывать entity IDs/hashes/IP/MAC/secrets.

## Приёмка Stage 69

Сделай `tests/evaluate_owner_acceptance_stage69.py`. Старые Stage 68 8/8 не считать достаточной приёмкой.

Обязательно проверить на **реальном текущем HA**, read-only кроме отдельно разрешённых control tests:

1. `Что сейчас делает Андрей, где он и сколько у него заряда?` — live status/area/battery.
2. Follow-up `а фильтр?` — текущее live значение, не значение из training file.
3. `Что с Roborock? Где он и сколько батареи?` — работает без предварительного learning profile.
4. `Что с роботом?` при двух роботах — clarification, а не молчаливый выбор Андрея.
5. `Что с посудомойкой?` — находит HA `Dishwasher` по человеческому русскому имени.
6. `включи посудомойку` — выбирает primary power только если цель однозначна; иначе уточняет.
7. transient switch `off -> on -> off` — НЕ verified.
8. stateless button — accepted_unverified, НЕ success.
9. вопрос `это Wi-Fi?` без causal evidence — причина не подтверждена; нельзя отвечать `да` или `нет` как факт.
10. Минимум несколько реальных `room + type` запросов (`свет <комната>`, `реле <комната>` и т.п.) должны разрешаться через текущий inventory без entity_id.

## После зелёной приёмки — только тогда learning

- Прочитай весь текущий HA inventory и построй/обнови semantic profiles для всех физических устройств.
- 4B teacher может предлагать semantic labels, но предложение не становится фактом без deterministic validation.
- Generated model answer никогда не является training fact.
- Owner corrections сохраняй как отдельный подтверждённый источник.
- Не запускай LoRA/QLoRA до доказанного повторяемого пробела, который не закрывается resolver/retrieval/grounding/verifier.

## Gates

Stage 69 нельзя объявлять завершённым, пока одновременно не выполнено:

- browser chat больше не обходит bounded HA path;
- Roborock и минимум 5 других заранее не обученных устройств читаются;
- `посудомойка` находится без слова `Dishwasher`;
- room/type queries работают без entity_id;
- live filter/resource value не теряется;
- 0 false-success;
- 0 actions по неоднозначной цели;
- accepted != verified на всех уровнях;
- полный offline suite зелёный;
- owner acceptance Stage 69 зелёный;
- реальный внешний Yandex skill отдельно проверен через опубликованный webhook/tunnel/настоящий запуск навыка, а не только POST на `127.0.0.1`.

В финале дай короткий отчёт на русском: что было сломано, что реально исправлено, какие живые сценарии прошли, latency, какие blockers остались. Не переходи к новым функциям до выполнения gates.
