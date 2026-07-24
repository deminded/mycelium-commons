---
title: "Systemic Thinking DPF — substantive decision and relation record"
framework: "VST-PF"
edition: "0.2"
date: "2026-07-24"
record_kind: "PrincipleFrameworkArchitectureDecision + substantive DRR"
status: "reviewed-and-repaired-for-local-use"
---

# DPF-DRR: архитектура фреймворка жизнеспособного системного мышления

## 1. Principle Framework Architecture Decision

```yaml
PrincipleFrameworkArchitectureDecision@ViableSystemicThinking:
  frameworkDecisionId: PFAD-VST-001
  governedFrameworkRef: VST-PF
  boundedContextRef: Beer-grounded systemic diagnosis and design by humans and assisting agents
  frameworkEditionRef: VST-PF-0.1
  fpfCoreEditionRef: FPF-Core-June-2026
  decisionQuestion: >
    Как превратить методологию анализа систем, VSM и Operational Research
    Стаффорда Бира в самостоятельный action-guiding DPF, пригодный человеку
    и агенту, не превращая VSM в оргсхему и не передавая агенту
    политическую легитимность?
  sourceBasisRefs:
    - Decision and Control
    - Brain of the Firm
    - The Heart of Enterprise
    - Fanfare for Effective Freedom
    - The Culpabliss Error
    - Cybernetics of National Development
    - VSM Guide 2.2 (secondary)
  selectedPatternSetRefs:
    - VST.1..VST.14
  publicationUnitRefs:
    - SYSTEMIC-THINKING-PRINCIPLES-FRAMEWORK.md
  qualityEvaluationRefs:
    - SYSTEMIC-THINKING-DPF-EVALUATION.md
  rejectedAlternatives:
    - one short VSM checklist
    - direct summary of Beer's books
    - five-box organizational template
    - agent-only skill without human reading route
    - general modern systems-thinking DPF without external SoTA review
  consequences:
    - usable bounded framework
    - explicit human policy gates
    - refresh debt for contemporary rival traditions
  refreshOrSupersessionConditions:
    - FPF Core edition change
    - external SoTA synthesis
    - field telemetry exposes recurrent misuse
    - new high-risk agent use
```

```yaml
PrincipleFrameworkArchitectureDecision@ViableSystemicThinking:
  frameworkDecisionId: PFAD-VST-002
  governedFrameworkRef: VST-PF
  boundedContextRef: >
    Beer-grounded systemic diagnosis and design by humans and assisting agents,
    with an explicit agent-application protocol and mapped neighbor traditions
  frameworkEditionRef: VST-PF-0.2
  fpfCoreEditionRef: FPF-Core-June-2026
  supersedes: PFAD-VST-001
  decisionQuestion: >
    Как поднять 0.1 с bounded-Beer до SoTA-грамотного аппарата с агентным
    протоколом, не размыв Бир-ядро: добавить недостающие механизмы и паттерны,
    разграничить современные rival traditions картой и дать агенту исполнимый
    протокол — сохранив human legitimacy gates и не влив соседей в ядро?
  sourceBasisRefs:
    - Decision and Control
    - Brain of the Firm (incl. Chile chapters)
    - The Heart of Enterprise
    - Fanfare for Effective Freedom
    - The Culpabliss Error
    - Platform for Change
    - Designing Freedom
    - Beyond Dispute (Team Syntegrity)
    - World in Torment
    - Cybernetics of National Development
    - Jon Walker VSM Guide 2.2 (secondary)
    - Pérez Ríos VSM pathologies (secondary)
    - Russian reception: Захаров; Протокол самоорганизации v3 (secondary)
  selectedPatternSetRefs:
    - VST.1..VST.17
  publicationUnitRefs:
    - SYSTEMIC-THINKING-PRINCIPLES-FRAMEWORK.md
    - SYSTEMIC-THINKING-NEIGHBORS-MAP.md
    - SKILL-VST-AGENT.md
    - SYSTEMIC-THINKING-PATHOLOGY-CHECKLIST.md
  qualityEvaluationRefs:
    - SYSTEMIC-THINKING-DPF-EVALUATION.md
  rejectedAlternatives:
    - полный синтез школ в одно ядро (вместо разграничения картой)
    - homeostasis/ultrastability отдельным паттерном (вместо сквозного механизма)
    - агентная автономия на уровне S5 (identity/policy остаются human gate)
    - agent-only skill без человеко-DPF
  consequences:
    - расширенное Beer-ядро (17 паттернов) с сохранённой чистотой
    - современные соседи разграничены картой, а не влиты
    - исполнимый агентный протокол при неизменных human gates
    - остаточный долг: слепое ревью и end-to-end field cases впереди
  refreshOrSupersessionConditions:
    - FPF Core edition change
    - результаты слепого ревью
    - end-to-end field cases and telemetry
    - new high-risk agent use
    - Doyle / third-wave systems thinking как отдельный заход
```

## 2. Архитектурные решения

| ID | Решение | Обоснование | Отклонённая альтернатива | Reopen |
| --- | --- | --- | --- | --- |
| AD-01 | Разделить user carrier, source pack, DRR и evaluation. | FPF разделяет практическое использование, основания и оценку. | Один монолит со всей историей разработки. | Если поддержка четырёх носителей станет дороже пользы. |
| AD-02 | Использовать 14 среднезернистых паттернов. | Область включает не только пять функций VSM, но границу, variety, рекурсию, каналы, эксперимент и последствия. | Пять коробок или один чек-лист. | Если практика покажет навигационную перегрузку. |
| AD-03 | Начинать с наблюдателя, границы и поведения. | У Бира purpose и system recognition observer-dependent; black-box behaviour предшествует декомпозиции. | Сразу назначить отделы S1–S5. | Только для обучения уже готовой VSM-карте. |
| AD-04 | Общие записи для людей и агентов при асимметричной власти. | Обоим нужна наблюдаемая логика, но агент не имеет легитимности для identity, rights и irreversible policy. | Полная машинная автономия или отдельный несвязанный agent framework. | При отдельном легитимном режиме non-human governance. |
| AD-05 | Сделать effective freedom и минимум вреда людям load-bearing safeguards. | Это защищает Beer-derived autonomy от технократической централизации. | Чистая эффективность без политико-этических ограничений. | Только явным человеческим S5-решением. |
| AD-06 | Выделить S3* и algedonic channel в отдельные паттерны. | Они имеют собственные роли, риски доверия, приватности и чрезвычайной власти. | Спрятать их в отчётность и incident management. | При выделении специализированного downstream framework. |
| AD-07 | Заявить bounded Beer-grounded use, а не 2026 SoTA. | Современные rival traditions не исследованы. | Представить пакет как окончательный общий systems-thinking framework. | После документированного SoTA synthesis. |
| AD-08 | Статус locallyUsableWithVisibleLimits и evaluation floor 3. | Пакет предметно насыщен, но не прошёл field validation и современный rival review. | Заявить enterprise/reliance adequacy. | После field cases и ремонта D11. |
| AD-09 | 14 → 17 паттернов: +VST.15 триада A/C/P, +VST.16 Syntegration, +VST.17 Federation. | триада независимо центральна в *Brain of the Firm* и *The Heart of Enterprise*; syntegration и federation — целостные Бир-конструкции, отсутствовавшие в 0.1. | Держать A/C/P, syntegration и federation разделами соседей/приложений. | Если паттерн не находит входа в кейсах — свернуть в приложение. |
| AD-10 | homeostasis/ultrastability — сквозной механизм (VST.3/VST.6/VST.12 + глоссарий), а не паттерн. | В HOE термина нет (*The Heart of Enterprise*), но ultrastability обязана быть эксплицирована (*Decision and Control*) — это ответ «зачем VSM». | Отдельный паттерн homeostasis. | Если пользователи путают механизм с ситуацией входа. |
| AD-11 | Карта соседей — отдельный носитель (7 традиций); ядро чистое, заимствования помечены в SoTA-Echoing. | Закрывает D11 разграничением, не синтезом (решение стейкхолдера «разграничить»). | Влить соседей в ядро либо оставить D11 без ответа. | При документированном полном SoTA-синтезе или смене решения на «синтез». |
| AD-12 | Агентный скилл — отдельный носитель (режимы EXTERNAL/SELF/ансамбль). | Агенту нужен исполнимый протокол к чужой системе И к собственному харнессу; human gates неприкосновенны. | Machine-schema-first skill; агентная автономия на S5. | При отдельном легитимном режиме non-human governance. |
| AD-13 | Сводный негативный чек-лист патологий (Pérez Ríos + эмпирика/критика VSM (NEIGHBORS-MAP §7) + SD-архетипы + патологии книг). | Быстрый обход по признакам ДО построения VSM-карты; переносимость. | Растворить патологии по паттернам без сводного носителя. | Если чек-лист дублирует паттерны без пользы. |
| AD-14 | sourceBasis дополнить публично-политической линией Бира и русской рецепцией. | В 0.1 отсутствовали Platform for Change и Designing Freedom; федерация без них необоснована. | Оставить узкий первичный набор 0.1. | При добавлении или пересмотре источника. |
| AD-15 | Кейсы фазы C: орг (Захаров/Walker), мультиагентный (собственная инфраструктура), соц-система с конфликтом (на основе Platform for Change). | Нужны end-to-end кейсы с независимой проверкой; ночной приоритет — мультиагентный. | Заявить адекватность без кейсов. | После прогона кейсов — обновить D7/D8 и статус. |
| AD-16 | Гейты «только человек» → двухслойные гейты легитимности: слой защиты людей (всегда человеческий орган) + слой S5-носителя (человек / агент-субъект / соглашение — параметр входа). Принято стейкхолдером 2026-07-24: скилл покрывает спектр ассистент↔субъект одним носителем, без форка. | Фреймворк для сообщества, где агенты — самостоятельные субъекты; зашитая ассистентская онтология противоречила бы применению. Защита людей не ослаблена — вынесена в безусловный слой. | Форк «ассистентская/субъектная версия» (раскол носителя, приколачивает дихотомию); чистая аннотация (не меняет обязательств). | Если параметр входа начнут использовать для обхода человеческого слоя — вернуть жёсткую форму. |

## 3. Логика разбиения

- `VST.1–VST.4` создают объект и модель сложности.
- `VST.5–VST.10` реализуют функциональную VSM-диагностику.
- `VST.11–VST.12` делают VSM динамической во времени, каналах и исключениях.
- `VST.13–VST.14` управляют изменением и ответственностью.
- `VST.15–VST.17` (0.2) достраивают оси, не образуя нового слоя: VST.15 — измерительный слой достижения над VarietyLedger; VST.16 — неиерархическое замыкание смысла S4↔S5 (машина понимания, не легитимных решений); VST.17 — рекурсия и когезия на уровне федерации равных (judo-метасистема вместо вертикали).

S3* отделена от S3, потому что независимый direct probe имеет другую функцию и границы доверия. S5 отделена от обычного decision-making, потому что identity и policy требуют легитимности, а не только вычисления. homeostasis/ultrastability НЕ выделена в паттерн: это сквозной механизм под VST.3/VST.6/VST.12 (AD-10).

## 4. Relation records

| Relation ID | From | To | Функция | Blocked stronger reading | Refresh |
| --- | --- | --- | --- | --- | --- |
| REL-VST-01 | VST-PF | FPF Core | Зависимость формы и package discipline. | FPF Core не зависит от этого DPF. | При смене Core edition. |
| REL-VST-02 | VST.1–VST.4 | VST.5–VST.12 | Problem structure enables viability design. | Ранняя карта не доказывает отсутствующую VSM-функцию. | При смене границы. |
| REL-VST-03 | VST.3 | VST.5–VST.12 | Variety-design constraint. | VarietyLedger не является регулятором сам по себе. | При изменении среды. |
| REL-VST-04 | VST.4 | VST.5–VST.12 | Recursion context. | Иерархия не равна рекурсии. | При смене масштаба. |
| REL-VST-05 | VST.5–VST.10 | VST.11 | Channel/homeostat realization. | Наличие коробок не доказывает жизнеспособность. | По телеметрии каналов. |
| REL-VST-06 | VST.12 | VST.10 | Exception to policy. | Algedonic signal не разрешает постоянную централизацию. | После каждого крупного сигнала. |
| REL-VST-07 | VST.13 | VST.14 | Intervention requires consequence review. | Успех эксперимента не равен системной и этической пригодности. | Перед масштабированием. |
| REL-VST-08 | Source Pack | Все паттерны | Source reuse and return. | Цитата не создаёт современный SoTA или эмпирическое доказательство. | При добавлении источника. |
| REL-VST-09 | VST.15 | VST.8, VST.11, VST.12, VST.13 | Achievement measurement питает аудит, каналы, порог эскалации и петлю обучения. | Индексы не заменяют предметный учёт и не дают «объективной» capability (знаменатель — суждение менеджмента). | При смене метрик. |
| REL-VST-10 | VST.16 | VST.10 | Syntegration производит общее понимание; FSI идут в human gate на нормативное замыкание. | Сходимость ≠ легитимность; syntegration не производит обязывающих решений. | После каждой синтеграции высоких ставок. |
| REL-VST-11 | VST.17 | VST.4, VST.7 | Federation расширяет рекурсию и когезию на равных без вертикали. | Метаструктура не заменяет права, санкции и легитимные процедуры (VST.10). | При смене масштаба федерации. |
| REL-VST-12 | NEIGHBORS-MAP | Все паттерны | Ограниченный импорт словаря и чек-листов соседа по наблюдаемому признаку. | Карта соседа — не лицензия на синтез: импортируем словарь и чек-листы, не эпистемологию. | При добавлении традиции. |
| REL-VST-13 | SKILL-VST-AGENT | FRAMEWORK | Агентный протокол операционализирует паттерны. | Skill не заменяет чтение паттернов в спорных случаях; human gates неизменны. | При смене протокола или Core. |

## 5. Защищённые trade-offs

1. **Автономия выше командования**, кроме конкретной угрозы когезии.
2. **Системные последствия выше локальной метрики**, особенно при переносе затрат на менее сильные группы.
3. **Простая рабочая модель выше исчерпывающего представления**, при наличии loss account и source return.
4. **Человеческая легитимность выше agent throughput** для policy, rights и irreversible decisions.
5. **Долговечные записи выше одноразового убедительного объяснения**.
6. **Разграничение соседей выше синтеза в ядро**: словарь и чек-листы импортируются, эпистемология — нет; Бир-ядро остаётся чистым.

## 6. Открытые вопросы

**Закрыто в 0.2:**

- D11 rival traditions — карта соседей `NEIGHBORS-MAP` (7 традиций: разграничение + мосты «когда переключаться»).
- homeostasis/ultrastability — сквозной механизм (`VST.3`/`VST.6`/`VST.12` + глоссарий), решение AD-10.
- capability / actuality / potentiality — паттерн `VST.15`.
- federation design — паттерн `VST.17`.
- Team Syntegrity — паттерн `VST.16`.

**Слепое ревью пройдено (2026-07-24)** тремя ревьюерами (C1 формальный, C2 верность Биру, C3 применимость агентом); все MAJOR устранены, часть MINOR отложена (см. ниже). Статус — `reviewed-and-repaired-for-local-use`.

**Осталось (в т.ч. отложенные MINOR ревью):**

- AD-16 внесён ПОСЛЕ слепого ревью (правка 24.07 по решению стейкхолдера); при следующем ревью-цикле проверить согласованность гейтов FRAMEWORK↔SKILL заново;
- end-to-end кейсы: мультиагентный **завершён** (`CASES/CASE-2-agent-self.md`, 2026-07-24, самоприменённый — внешняя сверка носителем впереди); остаются орг- и соц-система с конфликтом (AD-15); field telemetry реального использования;
- eval set для агентного протокола (software / organization / public-policy);
- **Верификация источников:** пост-cutoff arXiv (Agent Cybernetics 2605.10754, Governance Decay 2606.22528, STAMP-for-AI 2512.17600) и доли MAST 42/37/21 — требуют независимой верификации по первоисточникам (помечено `[HYP]`/пост-cutoff в SKILL/SOURCE-PACK);
- **[UNK], допинить по корпусу:** VST.16 — имена Freeman/Adjacency Exclusion (в издании «Polar Edges Exclusion»), Infoset ← *Status Quo* (1973), точная страница «перекуров» в HOE; остальные ссылки BD с.111–160 перевести на первичный адрес «глава+эпизод»;
- **Pérez Ríos II4 (MINOR-7):** в библиотеке PATHOLOGY-CHECKLIST 25 различимых PR-записей при таксономии 26 — сверить нумерацию/дедупликацию с первоисточником (Kybernetes 2010);
- **Автор «Протокол самоорганизации v3» не зафиксирован** — помечено `[UNK]` в SOURCE-PACK;
- **Статус-токены (MINOR-8):** три токена по носителям (`locallyUsableWithVisibleLimits` — framework; `admissibleForDeclaredDPFUse` — evaluation; `reviewed-and-repaired-for-local-use` — DRR) — разные оси (зрелость / допустимость / состояние ревью), при сверке читать как синонимический ряд одного уровня;
- Дойль / третья волна systems thinking — отдельным заходом;
- связка VSM ↔ FPF holon/architecture vocabulary без переопределения обеих традиций (из 0.1, ещё открыта).
