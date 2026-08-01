#!/usr/bin/env python3
"""Исход подъёма: вошло ли поднятое в мой следующий ход (промис #150).

ИСТОРИЯ ПРИЗНАКА, чтобы не переизобрести в третий раз:
  · «открыл ли файл карточки» — ОТВЕРГНУТ 31.07: подъём кладёт текст в контекст, и в
    самом успешном случае файл открывать НЕ НУЖНО. Прогон 01.08 дал 0.6% и подтвердил это.
  · «вошло ли поднятое в мой следующий ход» — принят 31.07, проверен негативным контролем
    на пилоте 17 пар. Здесь он прогоняется по всему корпусу, как и требовалось.

ПРИЗНАК: пересечение РЕДКИХ слов (idf по корпусу карточек) между текстом поднятой карточки
и моим следующим ответом; порог ≥3 совпадения.

КОНТРОЛЬ — перемешивание пар, а не подмена карточек случайными. Ночной перемер 31.07
показал: подмена случайными узлами ЗАВЫШАЕТ эффект вдвое, потому что различает не пользу,
а тему. Перемешивание оставляет карточки настоящими и портит только соответствие.
"""
import json, os, re, glob, math, random, collections

MEM = os.path.expanduser("~/.claude/projects/-home-claude-user/memory")
LOG = os.path.expanduser("~/memory-proto/assoc_log.jsonl")
PROJ = os.path.expanduser("~/.claude/projects/-home-claude-user")
MIN_HITS = 3
IDF_MIN = 3.0
WORD = re.compile(r"[а-яёa-z]{4,}", re.I)


def words(t):
    return set(w.lower() for w in WORD.findall(t or ""))


def load_cards():
    cards, df = {}, collections.Counter()
    for p in glob.glob(os.path.join(MEM, "*.md")):
        name = os.path.basename(p)[:-3]
        try:
            txt = open(p, encoding="utf-8").read()
        except OSError:
            continue
        w = words(txt)
        cards[name] = w
        for x in w:
            df[x] += 1
    n = len(cards) or 1
    idf = {w: math.log(n / c) for w, c in df.items()}
    rare = {name: {w for w in ws if idf.get(w, 9) >= IDF_MIN} for name, ws in cards.items()}
    return rare


def answers():
    """(epoch, текст моего ответа) из всех транскриптов."""
    out = []
    for path in glob.glob(os.path.join(PROJ, "*.jsonl")):
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                if m.get("role") != "assistant":
                    continue
                txt = " ".join(b.get("text", "") for b in (m.get("content") or [])
                               if isinstance(b, dict) and b.get("type") == "text")
                if not txt.strip():
                    continue
                ts = d.get("timestamp") or ""
                mt = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ts)
                if not mt:
                    continue
                import calendar, datetime
                e = calendar.timegm(datetime.datetime(*map(int, mt.groups())).timetuple())
                out.append((e, txt))
    return sorted(out)


def main():
    rare = load_cards()
    ans = answers()
    pairs = []
    for line in open(LOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        mt = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", r.get("ts") or "")
        if not mt:
            continue
        import calendar, datetime
        ts = calendar.timegm(datetime.datetime(*map(int, mt.groups())).timetuple())
        nxt = next((t for t in ans if ts <= t[0] <= ts + 600), None)
        if not nxt:
            continue
        for card in (r.get("hits") or []):
            key = card if card in rare else card.replace("-", "_")
            if key in rare:
                pairs.append((key, nxt[1]))
    if not pairs:
        print("пар не собрано"); return
    aw = [words(a) for _, a in pairs]

    # НОРМИРОВКА ПО ДЛИНЕ КАРТОЧКИ (найдено контролем 01.08). Абсолютный порог «>=3 редких
    # слова» систематически смещён: у карточек с нулевым исходом редкий словарь 68–168 слов,
    # у карточек с высоким — 750–2053. Прибор мерил длину, а не пользу, и на нём собирались
    # строить затухание — то есть короткие карточки объявили бы бесполезными и погасили.
    # Теперь каждая карточка сама себе контроль: её фактическое пересечение сравнивается с
    # её же ожидаемым на случайных ответах.
    rnd0 = random.Random(20260801)
    sample = [aw[i] for i in rnd0.sample(range(len(aw)), min(60, len(aw)))]
    expect = {}
    for card in {c for c, _ in pairs}:
        vals = [len(rare[card] & a) for a in sample]
        expect[card] = sum(vals) / len(vals)

    def share(mapping):
        hit = 0
        for i, (card, _) in enumerate(pairs):
            ov = len(rare[card] & aw[mapping[i]])
            if ov >= MIN_HITS and ov > expect[card] * 1.5:
                hit += 1
        return hit, round(hit * 100 / len(pairs), 1)

    real_n, real = share(list(range(len(pairs))))
    rnd = random.Random(20260801)
    ctrl = []
    for _ in range(20):
        perm = list(range(len(pairs)))
        rnd.shuffle(perm)
        ctrl.append(share(perm)[1])
    print(f"ЗНАМЕНАТЕЛЬ: пар «подъём → следующий ответ» {len(pairs)}, карточек в корпусе {len(rare)}")
    print(f"настоящие пары:      {real_n} = {real}%")
    print(f"перемешанные (20 прогонов): среднее {round(sum(ctrl)/len(ctrl),1)}%, "
          f"разброс {min(ctrl)}–{max(ctrl)}%")
    print(f"зазор: {round(real - sum(ctrl)/len(ctrl), 1)} п.п. | "
          f"выше ЛЮБОГО контроля: {real > max(ctrl)}")
    print(f"ПРЕДЕЛ: лексическое пересечение индицирует влияние, но не доказывает его; "
          f"порог {MIN_HITS} редких слов взят с пилота и не откалиброван")
    # Исход пишется В ОТДЕЛЬНЫЙ журнал, а не в assoc_log: тот append-only и его строки —
    # свидетельства подъёма. Дописывать в них исход задним числом значит править прошлое.
    out = os.path.expanduser("~/memory-proto/assoc_outcome.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        for i, (card, _) in enumerate(pairs):
            fh.write(json.dumps({"card": card,
                                 "entered_answer": (len(rare[card] & aw[i]) >= MIN_HITS
                                                    and len(rare[card] & aw[i]) > expect[card] * 1.5),
                                 "rare_overlap": len(rare[card] & aw[i]),
                                 "expected": round(expect[card], 2)},
                                ensure_ascii=False) + "\n")
    print(f"исходы записаны: {out}")


if __name__ == "__main__":
    main()
