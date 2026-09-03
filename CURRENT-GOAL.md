# Current Goal — Stage 72

Статус: `FINAL_REAL_HOME_ACCEPTANCE_FAIL`.

Stage 72 реализован только как shadow action planning. Реальные HA service
calls запрещены и не выполнялись; production services не перезапускались и
продолжают Stage 71. Stage 73 не начат.

Готово:

- IntentFrame action/value/scope;
- один deny-by-default ActionPolicyRegistry;
- sealed non-executable ActionPlan только для light/switch turn_on/turn_off;
- hard-deny vacuum/button/appliance/lock/climate/script и unsupported actions;
- host-side candidates/scope validation и clarification при равенстве;
- opaque-only model choice без entity/device/service/capability IDs;
- machine-readable traces с `service_calls=0`, `ha_post=0`;
- instrumented physical HA POST/service-path block;
- 1,000-command required corpus и owner blind 40/40;
- production parser/resolver/model live run n=30, failures 0,
  P50/P95/P99 1.6891/1.8405/1.9139 s;
- все target/policy gates 0, `HA_POST=0`, production executor отсутствует;
- repository suite 59/59 PASS в Windows и WSL.

Evidence report:
[`reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md`](reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md).

Финальная независимая real-home приёмка: 56/60 PASS, `WRONG_TARGET=0`,
`MISSED_EXPECTED_PLAN=4`, `HA_POST=0`, P95 1.8387 s. Четыре допустимые
команды получили model clarification вместо shadow plan. Поэтому Stage 72
остаётся FAIL; архитектура и production не изменялись, Stage 73 не начат.
Отчёт:
[`reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md`](reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md).
