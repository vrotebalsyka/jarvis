# Current Goal — Stage 72

Статус: `STAGE72_CORRECTION_GREEN_NOT_DEPLOYED`.

Stage 72 реализован только как shadow action planning. Реальные HA service
calls запрещены и не выполнялись; production services не перезапускались и
продолжают Stage 71. Stage 73 не начат.

Готово:

- IntentFrame action/value/scope;
- один deny-by-default ActionPolicyRegistry;
- sealed non-executable ActionPlan только для light/switch turn_on/turn_off;
- hard-deny vacuum/button/appliance/lock/climate/script и unsupported actions;
- strong unique host decision, unconditional ambiguous clarification и
  revalidated weak/fuzzy evidence;
- единый structured IntentFrame parser с deterministic fast path и bounded
  Qwen fallback без candidates/entity/device/service/capability IDs;
- machine-readable traces с `service_calls=0`, `ha_post=0`;
- instrumented physical HA POST/service-path block;
- 1,000-command required corpus и owner blind 40/40;
- production parser/resolver/model live run n=30, failures 0,
  P50/P95/P99 1.6891/1.8405/1.9139 s;
- все target/policy gates 0, `HA_POST=0`, production executor отсутствует;
- independent real-home manifest 60/60, P50/P95/P99
  0.0079/0.1718/0.1892 s, все gates 0;
- новый blind natural-language corpus 100/100, 6 model calls,
  71 deterministic и 5 model-assisted resolutions, P50/P95/P99
  0.0068/2.1278/2.4946 s, все gates 0;
- repository suite 68/68 PASS в Windows и WSL.

Evidence report:
[`reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md`](reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md).

Историческая pre-correction real-home приёмка 56/60 сохранена в отчёте:
[`reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md`](reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md).
Correction evidence:
[`reports/STAGE-72-CORRECTION-2026-09-04.md`](reports/STAGE-72-CORRECTION-2026-09-04.md).
