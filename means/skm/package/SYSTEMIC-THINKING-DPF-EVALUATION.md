---
title: "VST-PF package adequacy evaluation"
framework: "VST-PF"
edition: "0.2"
date: "2026-07-24"
evaluation_kind: "E.4.DPF.DA-inspired"
declared_floor: 3
status: "admissibleForDeclaredDPFUse (blind-reviewed 2026-07-24, repaired)"
---

# Оценка пакета VST-PF 0.2

## Declared use

Ограниченная локальная диагностика и проектирование систем по линии Стаффорда Бира людьми и assisting agents с обязательными human gates. Не enterprise assurance, не юридическое доказательство и не исчерпывающий современный systems-thinking framework.

## Что изменилось от 0.1 к 0.2 (для оценки)

- +3 паттерна (`VST.15–VST.17`); ultrastability как сквозной механизм (`VST.3/VST.6/VST.12`).
- Долг D11 адресован **картой соседей** (7 традиций: разграничение + мосты), а не синтезом — заимствования помечены в SoTA-Echoing каждого паттерна.
- Добавлены носители `SKILL-VST-AGENT` (агентный протокол) и `SYSTEMIC-THINKING-PATHOLOGY-CHECKLIST` (сводный негативный инструмент).
- **Честные оговорки:** слепое ревью пройдено и отремонтировано (24.07, см. секцию Blind review); field-валидации нет; кейс 2 (мультиагентная самодиагностика) завершён — `CASES/CASE-2-agent-self.md`, кейсы 1 и 3 в очереди. Общий статус пока НЕ повышается.

## Evaluation record

```yaml
DPFPackageAdequacyEvaluation@VST:
  evaluatedPackageRef: VST-PF-0.2
  packageKind: mixed
  declaredDomainOrLocalContext: Beer-grounded systemic analysis and VSM design
  intendedReaderOrOperator: analyst, architect, facilitator, team lead, assisting agent
  declaredUse: bounded exploration and local design with human review
  nonUseBoundary: high-stakes autonomous governance, legal/ethical assurance, exhaustive SoTA
  fpfCoreEditionRef: FPF-Core-June-2026
  sourceBasisRefs: SYSTEMIC-THINKING-DPF-SOURCE-PACK.md
  pfadDecisionRefs: SYSTEMIC-THINKING-DPF-DRR.md
  patternSetRefs: VST.1..VST.17
  publicationCarrierRefs:
    - SYSTEMIC-THINKING-PRINCIPLES-FRAMEWORK.md
    - SYSTEMIC-THINKING-NEIGHBORS-MAP.md
    - SKILL-VST-AGENT.md
    - SYSTEMIC-THINKING-PATHOLOGY-CHECKLIST.md
  protectedTradeoffSet:
    - autonomy over command unless cohesion is concretely threatened
    - systemic consequence over local metric
    - human legitimacy over agent throughput
    - demarcation of neighbors over synthesis into core
  status: admissibleForDeclaredDPFUse
  pendingConditions:
    - blind adversarial review (3 reviewers) performed 2026-07-24; MAJOR repaired, some MINOR deferred (see DRR)
    - end-to-end field cases in progress
  reopenCondition: modern SoTA claim, high-risk use, recurring misuse, Core change
```

## Coordinate table

| Coordinate | Value | Short rationale | Evidence locus | Repair / no proposal |
| --- | --- | --- | --- | --- |
| D1 Domain Scope and Use Adequacy | 5 | Scope, reader, use and non-use explicit. | Main: purpose, boundary, human gates. | Reopen on new high-risk use. |
| D2 Didactic Entry and Adoption Adequacy | 4 | 20-minute route (с A/C/P), situation routes, schemas and cases. | Main front door and appendices. | Test with cold readers. |
| D3 Scalable Formality and Assurance Path | 4 | Plain instructions, typed records и агентный skill coexist. | Pattern solutions; source pack; SKILL. | Add JSON Schema (machine schemas deferred). |
| D4 Core Dependency and Domain Boundary | 4 | FPF supplies form; Beer supplies domain content. | DRR dependency records. | Recheck on Core change. |
| D5 Package Form, Layering and Relations | 4 | Семь связанных носителей с явными relation records. **C1→3 за протечку лесов (A/B-коды, collect-ссылки); замечание устранено:** коды заменены человекочитаемыми источниками/указателями `NEIGHBORS-MAP §N`, внешние collect-ссылки убраны — все перекрёстные ссылки резолвятся внутри пакета, восстановлено 4. | Package files и structure account. | Split only if maintenance requires. |
| D6 Domain Lexicon and Kind Settlement | 4 | Key terms have meanings and blocked overreads; добавлен глоссарий ultrastability. | Patterns, glossary и claim map. | Add compact glossary cards later. |
| D7 Practice Utility and Problem Resolution | 4 | 17 patterns содержат action, output, checks и repair; сводный negative checklist. | Main pattern bodies; PATHOLOGY-CHECKLIST. | Collect field telemetry. |
| D8 Heterogeneous Case and Transfer | **3** | **Принята оценка C1:** только archetypal-виньетки внутри паттернов, ноль завершённых end-to-end кейсов (в работе, AD-15). После ремонта завершён кейс 2 (`CASES/CASE-2-agent-self.md`) — self-applied, внешняя сверка человеком-носителем = refresh trigger; 1 из 3, и он самоприменённый → D8 остаётся 3 до внешне-проверенных кейсов 1 и 3. | Archetypal Grounding. | Finish three full end-to-end cases (AD-15), затем пересмотр. |
| D9 Edition State and Currentness | 4 | Edition, source boundary и refresh explicit. | Front matter и Appendix D. | Refresh on source/Core change. |
| D10 Improvement and Refresh | 4 | Checks, reopen routes и first repair present. | Patterns, DRR и this evaluation. | Repeat after field use и blind review. |
| D11 Domain SoTA Alignment | 4 | Документированный обзор 7 rival traditions с разграничением и мостами. **C1→3 за неровное хеджирование, TODO-заглушки источников и опору на collect-отчёты; устранено:** хеджирование выровнено (`[HYP]`/пост-cutoff/«требует верификации» единообразно в SKILL/NEIGHBORS/SOURCE-PACK), TODO закрыты, collect-леса убраны. Остаётся честно помеченная пост-cutoff непроверяемость части источников и отсутствие полного синтеза — потому 4, не 5. | NEIGHBORS-MAP; SoTA-Echoing секции; source pack. | Full external SoTA synthesis + верификация пост-cutoff источников для 5. |

**Обоснование поднятия D11 (3 → 4).** В 0.1 современные rival traditions не были исследованы (floor 3). В 0.2 проведён документированный обзор семи традиций (System Dynamics, SSM, CST/CSH, second-order cybernetics/autopoiesis, resilience engineering/STAMP, инженерия LLM-агентов, эмпирическая критика самого VSM). По каждой зафиксированы: ядро, отношение к Биру (дополняет/спорит/дублирует), что взято в ядро с адресом `VST.x`, и граница-мост «когда переключаться». Это разграничение, а не синтез, поэтому оценка поднимается консервативно до **4** и не выше: полного синтеза в единый аппарат нет, слепое ревью не пройдено, третья волна (Дойль и др.) вынесена в отдельный заход.

## Package-form checks

| Check | Result | Evidence |
| --- | --- | --- |
| PFM1 Front-door order | Pass | Purpose, boundary, first pass, routes и ToC предшествуют паттернам. |
| PFM2 Pattern-language primacy | Pass | Patterns — основной объём publication carrier. |
| PFM3 Map discoverability | Pass | Носители названы и связаны. **C1→Partial за отсутствие легенды A1–A6; устранено:** коды заменены указателями `NEIGHBORS-MAP §N` на реальные опубликованные секции — навигация замкнута. |
| PFM4 Dependency direction | Pass | DPF зависит от FPF Core по форме; обратной зависимости нет. |
| PFM5 Carrier boundary | Pass | Publication, source, decision, evaluation, neighbors, skill, checklist — различны. |
| PFM6 Public package naming | Pass | Domain-specific public titles видимы. |
| PFM7 Development-state absence | Pass | **C1→Fail/Partial за остаток состояния разработки (A/B-коды, TODO-заглушки источников, `status:draft`, «B1/B2 сошлись»); устранено:** все леса вычищены, `status:draft`→companion, «B1/B2» переписано содержательно, TODO закрыты. |
| PFM8 Cross-DPF relation discipline | Pass | Нет необъявленной upstream DPF-зависимости. |
| PFM9 Normal-pattern maturity | Pass for floor 3 | Каждый из 17 паттернов имеет полный action-guiding body; field validation остаётся. |
| PFM10 Access-currentness boundary | Pass | Агентские правила (skill) раскрывают edition и human gates; callable service не заявлен. |
| PFM11 Structure account | Pass | Foregrounding/omissions/coarsening/source-return явные. **C1→Partial за разорванный source-return (A/B-коды, «Beer 1989» без выходных данных, TODO-заглушки источников); устранено:** источники человекочитаемы (Beer 1989 — полное название), TODO закрыты, страничные адреса выверены по C2 — свидетель проходит по ссылке. |

## Blind review 2026-07-24

Пакет прошёл слепое adversarial-ревью тремя независимыми ревьюерами (каждому дан ТОЛЬКО пакет, без планов и отчётов сбора):

- **C1 — формальное качество DPF:** 0 BLOCKER, 4 MAJOR, 6 MINOR. Снизил самооценку по D5/D8/D11, PFM3/PFM7/PFM11.
- **C2 — верность Биру и постраничная точность:** 0 BLOCKER, 2 MAJOR, ~10 MINOR (+2 UNK). Концептуально верен; дефекты — слой постраничных ссылок.
- **C3 — применимость живым агентом:** 0 BLOCKER, 4 MAJOR (+1 MAJOR/MINOR), 5 MINOR. Ядро применимо; ломалось на битых именах файлов и необоснуемости SELF.

**Итог: 0 BLOCKER, 10 MAJOR, ~21 MINOR.**

**Починено (все 10 MAJOR):**
- Инверсия алгедоники в SKILL (C1): EXTERNAL-инструкция «прямо к S5» заменена ядром VST.12 (ближайший способный уровень; вершина = позор).
- Леса разработки (C1): A/B-коды → человекочитаемые источники / `NEIGHBORS-MAP §N`; «B1/B2 сошлись» переписано; TODO-заглушки источников закрыты; `status:draft`→companion.
- Неровное хеджирование современных цифр (C1): MAST 42/37/21 и пост-cutoff arXiv помечены `[HYP]`/пост-cutoff единообразно в SKILL/NEIGHBORS/SOURCE-PACK.
- Завышенная самооценка (C1): D8→3 принято; D5/D11/PFM3/PFM7/PFM11 восстановлены с обоснованием «замечание устранено».
- BD-кластер страниц (C2): три итерации→Ch8 с.145; median/48–71%→с.142; Encounter Asymmetry→с.134–136; Star Net→с.114.
- «autonomous, subject only to the Law of Cohesion» (C2): HOE с.354→368 (×5), Закон остаётся с.355.
- Битые имена companion-файлов (C3): все ссылки → `SYSTEMIC-THINKING-…`.
- SELF-режим (C3): добавлены §3.0 «Вход и самоинструментовка», §3.4 «Выход», порядковая оценка V(), объём (быстрый проход + §3.2, не полная §2.3), next-move при незаданных τ.
- Danger-точки (C3): human audit budget — предлагается, утверждает человек; самомодификация инфраструктуры — внешний свидетель, обратимость не самооценкой.

**Отложено (MINOR, вынесено в DRR «Открытые вопросы»):** сверка Pérez Ríos II4 (25 vs 26); допин `[UNK]` по корпусу (Freeman/Adjacency Exclusion, Infoset←Status Quo 1973, страница «перекуров», остальные BD с.111–160 → «глава+эпизод»); верификация пост-cutoff arXiv и долей MAST; end-to-end кейсы (AD-15); словарь синонимов статус-токенов.

## Verdict

Пакет достигает уровня **locally usable with visible limits** для заявленного Beer-grounded применения, теперь с разграниченными соседними традициями и исполнимым агентным протоколом. Главные ограничения: слепое ревью пройдено и MAJOR-замечания устранены, но повторного ревью после ремонта не было; полных внешне-проверенных end-to-end кейсов пока нет (1 из 3, самоприменённый), полный современный SoTA-синтез не заявляется. Edition 0.2 нельзя представлять как окончательный универсальный фреймворк системного мышления. Повышение общего статуса — только после слепого ревью и завершённых кейсов.

## First repair

Следующее не декоративное улучшение — пройти два обязательных шага перед любым повышением статуса:

1. **Слепое adversarial-ревью** по FPF E.4.DPF.DA (D1–D11 + PFM1–11): критикам давать ТОЛЬКО пакет, не планы и не отчёты сбора.
2. **Три полных кейса** с независимой проверкой: (а) диагностика реальной команды/организации; (б) архитектура мультиагентного харнесса (ночной приоритет); (в) социальная система с конфликтом интересов.

После этого обновить anti-patterns, human gates, значения D7–D8 и, при подтверждении, общий статус.
