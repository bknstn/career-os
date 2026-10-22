# Career Radar R1 — контракт ролей и тренда

**Статус:** реализованный контракт сохранённых R1 builds. Достаточность данных
проверяется отдельно для каждого результата. Контекст решения:
[ADR 0010](adr/0010-pivot-to-career-radar.md).
**Назначение:** узкий первый срез по role families, определяющий, когда
наблюдаемую долю можно сравнить. Это не API и не прогноз.

## Граница утверждения

R1 отвечает на вопрос: «Какую долю уникальных postings из нашей неизменной панели
составляли postings данной технической роли в каждом из двух периодов
наблюдения?» Он не утверждает, что это весь рынок труда, число новых вакансий,
темп найма, длительность открытий, устойчивый спрос или причинное влияние
технологий.

**Основная метрика — distinct observed-posting share за окно.** Каждое 90-дневное
окно делится от его левой границы на тринадцать последовательных семидневных
слотов (последний может быть короче). Для каждого board/слота берётся первый по
`(finished_at, stable run_id)` успешный полный scan; его `observed_at` равен
`finished_at`, и он становится sample этого слота в immutable R1 build,
идентифицируемом panel digest, точными границами
окон, source и slot ordinal. Assignment — часть конкретного build и не является
глобальной записью на board/week. Rolling cutoff создаёт другие границы и может
выбрать другие samples. Остальные более поздние scans в том же слоте не влияют
на R1. Это удерживает одинаковую частоту семплирования, даже если сбор запускают
чаще.

Для пары окон используются только совпадающие `board × slot ordinal`, где есть
sample в обоих окнах. Сначала для каждого окна собирается множество postings из
выбранных sample scans этих matched slots; повторные наблюдения одного posting
внутри окна схлопываются. Затем берётся последняя выбранная observation posting
в окне и классифицируется её содержимое. Так coverage пропуска в одном окне не
превращается в нулевую вакансию для сравнения. Дополнительные scans не
увеличивают числа. Это всё ещё мера наблюдённой распространённости postings в
выбранных weekly snapshots, не поток новых открытий и не stock на конец окна;
ATS может не публиковать вакансии между snapshots или не показывать их дольше.

Текущий stock — отдельное состояние: полный набор postings в последнем
успешном полном скане каждого board до cutoff. Его нельзя смешивать с оконной
метрикой.

## Universe, панель и знаменатели

1. **Posting identity:** `(source_key, external_id)`. Дедупликация между ATS,
   boards или компаниями в R1 отсутствует.
2. **Фиксированная панель:** версионированный неизменяемый manifest board-ов,
   содержащий стабильный `source_key`, `company_key`, ATS kind и digest
   относящейся к сбору конфигурации. `company_key` — явно утверждённая стабильная
   идентичность для расчёта концентрации: boards одной компании могут иметь
   общий ключ, но postings между boards не сливаются. Сравнение допустимо лишь
   при одном manifest в обоих окнах. Добавленные boards получают новый baseline;
   onboarding компании не приписывается рыночному росту.
3. **Начальный roster role families:** `backend_engineering`,
   `frontend_engineering`, `fullstack_engineering`, `data_engineering`,
   `product_engineering`, `platform_infrastructure_engineering` (включая
   SRE/DevOps), `machine_learning_engineering`, `ai_engineering`,
   `ai_product_engineering`,
   `solutions_architecture` и `engineering_management`. `unclassified` — явное
   состояние, не семейство. `Engineering Manager` относится к
   `engineering_management`; `Tech Lead`, `Staff` и `Principal` сохраняют
   family основной работы. Явный `Data Engineer` относится к
   `data_engineering`; `ML Engineer`, `AI Engineer` и `AI Product Engineer`
   остаются разными семьями; общий `Product Engineer` относится к
   `product_engineering`. P2 уточняет детерминированные признаки этих групп и
   golden examples; расширение списка требует отдельного основания.
4. **Scope и precedence:** явно нетехническая должность получает
   `out_of_scope`; явная техническая, но неоднозначная family остаётся
   `unclassified`; если из записи нельзя понять даже scope, ставится
   `unknown_scope`. `out_of_scope` — единственное состояние, исключённое из
   denominator. Неизвестный/пустой/неоднозначный title и `unknown_scope`
   включаются в denominator как `unclassified`, а не исчезают молча. При
   классификации role явное название работы в title имеет приоритет; если title
   общий, может помочь описание. Конфликт свидетельств даёт `unclassified`.
   Seniority и контекст не меняют role family; слово `AI` само по себе —
   контекст, а не роль. Для каждого posting сохраняется максимум одна role
   family.
5. Для каждого posting в окне сначала выбирается последняя observation из
   выбранных matched sample scans, затем применяется scope/classifier. Если
   posting сменил статус или role и последняя observation стала
   `out_of_scope`, он исключается именно из этого окна; если стала
   `unknown_scope`/`unclassified`, остаётся в denominator.
6. Для окна `W` denominator `N(W)` — число уникальных source postings, чья
   последняя observation после этого правила не равна `out_of_scope`.
   Numerator `R(W, role)` — уникальные postings из `N(W)`, классифицированные
   ровно в одну такую role family. `unclassified`/`unknown_scope` входят в `N(W)`
   и в отдельный unclassified count, но не в числители ролей.
7. `share(W, role) = R(W, role) / N(W)`. Наблюдённые `R=0` и `share=0%` при
   `N>0` остаются валидными описательными значениями независимо от quality state;
   quality gate решает, можно ли вычислить сравнение. Сравнение выводит обе доли
   и только разность в процентных пунктах
   `delta_pp = 100 × (share(current) - share(previous))`. При `N(W)=0` доля
   равна `null`.

В per-run membership `observed_at` равен `CollectionRun.finished_at` успешного
полного scan; это время завершённого наблюдения, не `started_at` и не timestamp
отдельного posting. Зафиксированные поля `first_seen_at`, `last_seen_at`,
`status` и `published_at` сами по себе не дают принадлежность к каждому окну. В период
попадает только per-run observation membership с зафиксированным UTC временем
успешного полного скана. `first_seen_at` не считается датой публикации.
`published_at` можно показывать отдельно как source-provided evidence с coverage,
но он не определяет окна и не восстанавливает отсутствующую историю.

## Время и lifecycle

Cutoff `T` — явно сохранённый timezone-aware UTC instant; все периоды
полуоткрытые. Длительность равна 90×24 часа:

```text
previous = [T − 180 days, T − 90 days)
current  = [T −  90 days, T)
```

Observation с timestamp ровно на правой границе относится только к следующему
окну. Для каждой пары `(posting, window)` выбирается последняя observation по
`observed_at`, затем стабильный run ID для разрешения равного timestamp.

`active` — последнее подтверждённое состояние в source snapshot, а не
непрерывный исторический интервал. Только успешный полный scan вправе
подтвердить отсутствие posting на конкретном скане. Partial, failed, running или
устаревший скан не означает пустую компанию. Изменение content создаёт
свидетельство наблюдения, но не новую вакансию; отсутствие и последующая
реактивация не дают надёжного открытия/закрытия интервала в текущей схеме.
Поэтому R1 не считает openings, closures, active-days, reactivation flow или
velocity.

## Сопоставимость и quality gates

Ожидаемая cadence зафиксирована для R1: минимум один полный scan каждого board
за каждый семидневный слот. Успешный пустой полный scan считается покрытием,
failed/partial — нет. Для сравнения используются только matched board/slot
pairs по ordinal позиции двух окон. Hard gates:

- одинаковый manifest панели, role scope и версии правил классификации;
- matched pairs составляют не менее 90% всех ожидаемых `board × slot` пар в
  целом и не менее 80% слотов каждого board;
- `unclassified`/`unknown_scope` не выше 20% denominator в каждом окне;
- в каждой role family, для которой показывается delta, не менее 20 postings и
  не менее 5 разных компаний в каждом окне; доля крупнейшей компании в этих
  postings не выше 40% в каждом окне.

`run_quality_state` — детерминированно `provisional`, если panel, matched
coverage и classifier gates пройдены для обоих окон; иначе `insufficient`.
Отдельный `role_quality_state` равен `provisional`, если run gates и все sample,
company-count и concentration gates данной role family пройдены в обоих окнах;
иначе `insufficient`. `provisional` значит пригодно для описательного пилота;
это не статистическая уверенность и не допуск к прогнозу. Малая выборка одной
family не понижает quality state другой family.

Наблюдённые counts `R`, `N` и shares показываются с quality state даже при
недостатке выборки/coverage; они не скрываются min-20 gate. При не пройденном
run-level coverage gate числа остаются coverage-conditioned и помечаются
`insufficient`. `delta_pp` для role family остаётся `null`, пока run и её role
gates не пройдены. Подтверждённый нулевой count показывается как `0`; при `N>0`
share равна `0%`, а `role_quality_state` будет `insufficient` из-за sample
threshold. Ненаблюдаемая из-за отсутствия истории величина — `null`, не `0`. При
известном `N=0` share и delta `null` с `zero_denominator`.

Пороговые `20 postings / 5 companies / 40%` взяты как стартовые описательные
пороги из исторического D5 quality review (`docs/MARKET_DATA_REFRESH_2026-08-24.md`)
и применяются консервативно отдельно к каждой role family и обоим окнам. Это
не статистическая гарантия и не доверительный интервал. Порог `unclassified`
и matched weekly coverage приняты reviewer как начальные описательные gates
пилота; это не эмпирически подтверждённые пороги рыночной достоверности. Непройденный gate даёт `insufficient` + gate code, без оценки
направления. Успешный gate всё равно разрешает только описательную долю и
разницу, не причинное или прогнозное утверждение.

Причины, блокирующие сравнение: `missing_history`, `panel_mismatch`,
`coverage_unknown`, `coverage_below_threshold`, `classifier_version_mismatch`,
`unclassified_above_threshold`, `sample_below_threshold`,
`company_count_below_threshold`, `company_concentration_above_threshold`,
`zero_denominator`. В выводе сохраняются значения пройденных/непройденных gates.

## Версионированный результат

JSON результата хранит: `T`, обе границы, длительность и определение
метрики; panel manifest, `manifest_digest` и `panel_digest`; для каждого окна
полный список ожидаемых `board × slot`, выбранный run ID либо причину пропуска,
matched-pair mapping, coverage и input digest; denominator, numerator, share и
процентно-пунктовую разницу по семействам; `unclassified`/`out_of_scope`
coverage; company concentration;
normalized cutoff; версии classifier, taxonomy и metric contract; digest набора
inputs; `run_quality_state`, отдельный `role_quality_state` для каждой family
и коды всех quality gates. Нельзя опускать пустые/неизвестные категории из
denominator или выдавать `insufficient` за 0%.

До отдельного решения R1 не присваивает сигналам публичные ярлыки
`accelerating`, `stable` или `cooling`; это оставляет сырые описательные
результаты проверяемыми, а семантические ярлыки — следующему quality gate.

## Минимум воспроизводимых сохранённых входов и реализации

Нынешних `collection_runs` недостаточно: они хранят counters и статус, но не
posting membership. Для нового R1 baseline нужны только:

- небольшой проверяемый file-backed panel manifest, например
  `config/radar/panel.v1.yaml`, с `panel_version` и явным списком
  `(source_key, company_key, source_kind)`. Он передаётся в существующую команду
  `career-os collect --config <sources.yaml> --radar-panel-manifest <manifest>`;
  отдельная команда регистрации или сервис не нужны. До сетевого сбора
  проверяются уникальность ключей, наличие каждого source в выбранной source
  config, совпадение kind и непустой стабильный `company_key`. Сохраняются два
  digest: `manifest_digest` — SHA-256 canonical JSON проверенного файла без
  зависимости от YAML formatting/path; `panel_digest` — составной digest
  manifest и соответствующих source-config digests. Существующий source config
  остаётся единственным местом настройки ATS. Повторное использование
  `panel_version` при изменении `manifest_digest` или любого source-config
  digest отклоняется до collection; такое изменение требует новой panel version
  и baseline;
- автоматическая запись collection для каждого manifest source — run ID,
  manifest/panel digests, terminal completeness/status, `finished_at`, posting
  membership `(run_id, source_key, external_id)` с `observed_at = finished_at` и
  неизменяемые минимальные classifier inputs `title` и `description` на этой
  observation. Успешный полный
  scan, включая пустой, атомарно сохраняет membership и статус `succeeded`;
  только он может подтверждать отсутствие и быть sample. Partial/failed/running
  runs сохраняют статус, но не используются как sample. Membership пишется при
  каждой полной observation, даже если canonical hash не изменился. Это не
  опирается на mutable `raw_latest` или `JobVersion` как журнал observations;
- при каждом R1 build — детерминированно выбранные run IDs и matched `board ×
  slot ordinal` в immutable run manifest. Assignment ограничен ключом
  `(panel digest, window boundaries, source, slot ordinal)`; более частые scans
  после первого не заменяют sample, а другой cutoff может иметь другие слоты;
  глобальная таблица назначения не нужна;
- versioned classification result с role/eligibility, причиной или evidence
  и classifier/taxonomy version. Повторный вывод не читает текущие mutable
  enrichment как историческую истину;
- immutable R1 run manifest с cutoff, границами, panel digest, входным digest,
  версиями алгоритма, coverage/gates и агрегатами.

Observation persistence должна допускать повтор content после цикла A→B→A и
повторные неизменённые source observations; текущий unique
`(job_id, content_hash)` для этого недостаточен. Также нельзя принимать
изменяемое taxonomy enrichment за immutable job version. Полный копируемый архив
всей БД или второй backend для этого не нужны.

CLI строит один run из замороженного входного набора: транзакция сохраняет run
manifest, gate outcomes и все строки агрегатов целиком или откатывает их целиком.
Идемпотентный ключ образуют metric contract version, cutoff/window boundaries,
panel digest, input-membership digest, classifier/taxonomy versions и aggregation
algorithm version. Повтор тех же входов возвращает тот же сохранённый результат
без дублирования; отличающийся input digest или версия создаёт
отдельный immutable build, а не перезаписывает прежний. JSON печатается только
после успешного commit. Сбой не оставляет видимого частичного Radar run.

### CLI contract

R1 добавляет группу Typer `radar` и ровно две команды; manifest остаётся
валидируемым входным файлом существующего collection flow:

```text
career-os collect --config <sources.yaml> --radar-panel-manifest <manifest.yaml>
career-os radar build --panel-manifest <manifest.yaml> --cutoff-utc <RFC3339-UTC>
career-os radar show --run-id <uuid>
```

Опциональный `--radar-panel-manifest` валидируется до collection и при успешном
collect автоматически связывает manifest с complete-scan membership; P3 не
требует отдельной команды seed/registration. `build` принимает тот же файл,
сверяет его `manifest_digest`, затем использует ранее сохранённый `panel_digest`
и source-config provenance. Он читает только сохранённые scan samples и
manifest, не запускает ATS collection.
Manifest, который ещё не был использован collection, является невалидной
версией для build. Валидный зарегистрированный manifest при нехватке retained
history даёт сохранённый `insufficient` result. Cutoff должен включать `Z`/UTC
offset и сохраняется нормализованным в UTC. `build` пишет immutable run и
печатает его JSON после commit. `show` возвращает сохранённый JSON run и не
перечитывает mutable `JobPosting` rows. Для всех команд JSON — стандартный
формат без флага.

Exit codes: `0` — run успешно сохранён/прочитан, включая quality state
`insufficient`; `1` — БД, persistence или вычислительная ошибка (при `build`
транзакция откатывается); `2` — неверные аргументы, невалидный cutoff или
manifest/version; `4` — запрошенный `show --run-id` не найден. Отсутствие
достаточной исторической выборки — валидный сохранённый `insufficient` run, а не
ошибка процесса.

### Acceptance cases для P3

Эти проверки обязательны до review P3; здесь зафиксированы ожидаемые результаты,
реализация и тесты ещё не добавлены:

- observations точно на `T−180d`, `T−90d` и `T`: левая граница включена,
  правая исключена; `T−90d` не дублируется между окнами;
- доли `.10 → .12` дают `delta_pp = +2`, а не `.02`;
- posting с повторными observations и несколько успешных scans в одном slot
  учитывается по первому `(finished_at, run_id)` scan; добавление более позднего
  redundant scan не меняет counts; matched pairs повторяют выбор в обоих окнах;
- последняя observation в окне выбирается до scope/role classification:
  переход `in_scope → out_of_scope` исключает posting из этого окна, обратный
  переход включает, ambiguous scope остаётся в unclassified denominator;
- повторные неизменённые observations и content `A→B→A` сохраняются без потери
  evidence/conflict; повторная сборка того же input/версий даёт тот же run,
  новые versions дают отдельный immutable run;
- полный пустой scan даёт coverage; partial/failed scan не доказывает отсутствие;
  реактивация не создаёт задним числом интервалы открытия/закрытия;
- только старые отчёты, `first_seen_at` или `published_at` без R1 scan samples
  дают `status=insufficient`, `reason=missing_history`, null history counts,
  никогда не реконструированные нули/дельты;
- известные `N>0, R=0` показывает count `0` и share `0%`, но скрывает
  `delta_pp` при непройденном sample gate; `N=0` даёт null share с
  `zero_denominator`;
- quality thresholds дают детерминированный `provisional` только при прохождении
  всех run-level gates; неполное покрытие или доля unclassified дают
  `run_quality_state=insufficient`. Недостаточный sample, число компаний или
  концентрация дают `role_quality_state=insufficient` для соответствующей family
  и видимые raw counts;
- ошибка при сохранении manifest/details/aggregate оставляет ноль частичных run
  rows. PostgreSQL integration case выполняется на disposable DB с проверкой
  rollback; обычный `make check` с DB skips не заменяет её.
- `build` сохраняет `insufficient` и завершает процесс с кодом 0 при queryable
  БД, даже когда нет истории; сбой подключения/записи завершает кодом 1 без
  partial rows. Повтор `show` возвращает те же сохранённые байтово-stable JSON
  values; неизвестный run ID возвращает код 4.
- panel manifest отклоняется до сети при duplicate/unknown/mismatched source;
  повторное использование версии с другим manifest или source-config digest
  отклоняется до запуска collection;
  successful full collection автоматически записывает точное membership для
  каждого manifest source, включая пустое, а partial/failed collection не
  становится sample. Повторная неизменённая collection всё равно сохраняет
  новое observation membership.

## Архитектурное решение

Причины сохранения неизменяемых наблюдений и ограничения ретроспективных
сравнений записаны в [ADR 0010](adr/0010-pivot-to-career-radar.md).
