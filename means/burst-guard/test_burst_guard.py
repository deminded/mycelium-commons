# -*- coding: utf-8 -*-
"""Оракул страховки от залпа. Случай-повод: 01.10.2026, ~30 пересланных сообщений
по 8–10 тыс. знаков переполнили окно агента. Запуск: python3 -m pytest -q"""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from burst_guard import BurstGuard, Decision, DEFAULT_BOUNCE, DELIVER, FOLD, HOLD, MUTE   # noqa: E402

CHAT, OTHER = "100000001", "-1000000000002"
BIG = ("строка лога хода " * 260)[:4000]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def guard(tmp_path, clock):
    return BurstGuard(tmp_path / "folded", clock=clock, overflow_chars=20_000)


def burst(guard, chat=CHAT, n=30, **kw):
    return [guard.check(chat, 8759 + i, BIG, **kw) for i in range(n)]


def test_single_message_passes_whole(guard):
    d = guard.check(CHAT, 1, BIG)
    assert d.action == DELIVER and d.text == BIG and d.notice is None and d.bounce is None


def test_budget_boundary_is_inclusive(tmp_path, clock):
    g = BurstGuard(tmp_path, budget_chars=10, clock=clock)
    assert g.check(CHAT, 1, "x" * 10).action == DELIVER
    assert g.check(CHAT, 2, "y").action == FOLD


def test_burst_folds_and_bounds_window(guard):
    ds = burst(guard)
    assert {d.action for d in ds} == {DELIVER, FOLD}
    window = sum(len(d.text) + len(d.notice or "") for d in ds)
    # без страховки было бы 30 × 4000; со страховкой — бюджет плюс короткие извещения
    assert window < guard.budget_chars + 30 * 600
    assert len(ds) == 30, "ни одно сообщение не пропало"


def test_folded_text_lands_in_file_whole(guard):
    ds = burst(guard)
    folded = [d for d in ds if d.action == FOLD]
    assert folded
    for d in folded:
        assert Path(d.path).read_text(encoding="utf-8") == BIG
        assert d.path in d.notice
        assert stat.S_IMODE(os.stat(d.path).st_mode) == 0o600
        assert len(d.text) < guard.preview_chars + 40


def test_other_chat_not_folded_by_neighbour_burst(guard):
    burst(guard)
    d = guard.check(OTHER, 1, BIG)
    assert d.action == DELIVER and d.text == BIG


def test_window_passes_and_full_delivery_returns(guard, clock):
    burst(guard)
    clock.t += guard.window_s + 1
    assert guard.check(CHAT, 99, BIG).action == DELIVER


def test_same_message_id_twice_keeps_both_texts(guard):
    burst(guard)
    a = guard.check(CHAT, 7, "первая редакция " + BIG)
    b = guard.check(CHAT, 7, "вторая редакция " + BIG)
    assert a.path != b.path
    assert Path(a.path).read_text(encoding="utf-8").startswith("первая")
    assert Path(b.path).read_text(encoding="utf-8").startswith("вторая")


def test_hostile_ids_stay_inside_fold_dir(guard):
    burst(guard)
    d = guard.check("../../etc", "../passwd", BIG + "x" * 40_000)
    assert d.action == FOLD
    assert Path(d.path).resolve().is_relative_to(guard.fold_dir.resolve())


# --- файл не записался: требование Дмитрия К (02.10.2026) с двумя условиями

@pytest.fixture
def broken(tmp_path, clock):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    yield BurstGuard(ro / "folded", clock=clock, overflow_chars=20_000)
    ro.chmod(0o700)


def test_write_failure_holds_instead_of_flooding(broken):
    if os.geteuid() == 0:
        pytest.skip("root пишет в каталог 0500")
    ds = burst(broken)
    held = [d for d in ds if d.action == HOLD]
    assert held, "без файла сообщение не должно приходить целиком"
    for d in held:
        assert BIG not in d.text and d.path is None


def test_bounce_once_per_chat_per_window_in_dm(broken, clock):
    if os.geteuid() == 0:
        pytest.skip("root пишет в каталог 0500")
    ds = burst(broken)
    bounces = [d.bounce for d in ds if d.bounce]
    assert bounces == [DEFAULT_BOUNCE], "сто сообщений залпа — один ответ"
    clock.t += broken.window_s
    assert broken.check(CHAT, 1, BIG + "x" * 40_000).bounce == DEFAULT_BOUNCE


def test_group_bounces_only_when_addressed(broken):
    if os.geteuid() == 0:
        pytest.skip("root пишет в каталог 0500")
    silent = burst(broken, chat=OTHER, is_group=True)
    assert not any(d.bounce for d in silent), "на чужой залп в группе молчать"
    called = broken.check(OTHER, 1, BIG, is_group=True, addressed=True)
    assert called.action == HOLD and called.bounce == DEFAULT_BOUNCE


def test_bounce_text_is_configurable(tmp_path, clock):
    if os.geteuid() == 0:
        pytest.skip("root пишет в каталог 0500")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        g = BurstGuard(ro / "f", clock=clock, bounce_text="Too long, send a file.")
        ds = burst(g)
        assert [d.bounce for d in ds if d.bounce] == ["Too long, send a file."]
    finally:
        ro.chmod(0o700)


# v2: исходные проверки выше сохранены; их 30 свёрток используют явную надбавку.
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace
import burst_guard as bg


def window_text(d):
    return "\n".join(part for part in (d.notice, d.text) if part)


def default_guard(tmp_path, clock, **kw):
    return BurstGuard(tmp_path / "default", clock=clock, **kw)


@pytest.mark.parametrize("tiny,failed", [(False, False), (True, False), (False, True)])
def test_long_stream_has_exact_hard_bound(tmp_path, clock, tiny, failed):
    folder = tmp_path / "folds"
    if failed:
        folder.write_text("blocked")
    g = BurstGuard(folder, clock=clock)
    texts = ["x" * 4000] * (10 if tiny else 1000)
    if tiny:
        texts += ["x"] * 1000
    ds = [g.check(CHAT, i, text) for i, text in enumerate(texts)]
    fields = sum(len(d.text) + len(d.notice or "") for d in ds)
    rendered = sum(len(window_text(d)) for d in ds)
    assert fields <= rendered <= g.budget_chars + g.overflow_chars
    assert g._used(CHAT, clock.t) == rendered
    assert any(d.action == MUTE for d in ds)
    assert sum(d.notice is not None for d in ds if d.action == MUTE) == 1
    for d, text in zip(ds, texts):
        if d.action == MUTE:
            assert d.text == ""
        if d.path:
            assert Path(d.path).read_bytes() == text.encode("utf-8")
        if not failed and d.action != DELIVER:
            assert d.path is not None, "каждое успешное сохранение обязано вернуть файл"
            assert Path(d.path).read_bytes() == text.encode("utf-8")
        if failed:
            assert d.path is None
    if not failed:
        assert len(list(folder.rglob("*.txt"))) == sum(d.action != DELIVER for d in ds)
    if failed:
        assert sum(d.bounce is not None for d in ds) == 1


def test_full_file_bytes_beyond_4000_and_unique_tail(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=0)
    text = "юникод\n" * 1200 + "UNIQUE_END_б42"
    d = g.check(CHAT, 1, text)
    assert d.action == FOLD
    assert Path(d.path).read_bytes() == text.encode("utf-8")


def test_accounting_includes_notice_and_joiner(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=0)
    d = g.check(CHAT, 1, BIG)
    assert d.action == FOLD
    assert g._used(CHAT, clock.t) == len(window_text(d))
    assert isinstance(g._sent[CHAT], deque)


def test_budget_at_half_and_exact_expiry(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=10)
    assert g.check(CHAT, 1, "x" * 10).action == DELIVER
    clock.t += g.window_s / 2
    half = g.check(CHAT, 2, "x")
    assert half.action == FOLD
    clock.t = 1000 + g.window_s - 0.001
    assert g._used(CHAT, clock.t) >= 10
    clock.t = 1000 + g.window_s
    # Сообщение середины окна ещё учтено: проверяем отдельно точную исходную границу.
    assert g._used(CHAT, clock.t) == len(window_text(half))
    fresh = default_guard(tmp_path / "fresh", Clock(), budget_chars=10)
    fresh.check(CHAT, 1, "x" * 10)
    fresh._clock.t = 1000 + fresh.window_s - 0.001
    assert fresh._used(CHAT, fresh._clock.t) == 10
    fresh._clock.t = 1000 + fresh.window_s
    assert fresh.check(CHAT, 2, "y" * 10).action == DELIVER


def test_all_sliding_windows_with_staggered_arrivals(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=1500, overflow_chars=2000)
    history = []
    for i in range(240):
        clock.t += 3.1
        d = g.check(CHAT, i, "x" * (300 + i % 7 * 400))
        history.append((clock.t, len(window_text(d))))
        actual = sum(n for t, n in history if clock.t - t < g.window_s)
        assert actual <= g.budget_chars + g.overflow_chars
        assert actual == g._used(CHAT, clock.t)


def failing_guard(tmp_path, clock, **kw):
    blocked = tmp_path / "blocked"
    blocked.write_text("blocked")
    return BurstGuard(blocked / "bad", clock=clock, budget_chars=0, **kw)


def test_bounce_dm_half_window_and_boundary(tmp_path, clock):
    g = failing_guard(tmp_path, clock)
    assert g.check(CHAT, 1, BIG).bounce == DEFAULT_BOUNCE
    clock.t += g.window_s / 2
    assert g.check(CHAT, 2, BIG).bounce is None
    clock.t = 1000 + g.window_s - 0.001
    assert g.check(CHAT, 3, BIG).bounce is None
    clock.t = 1000 + g.window_s
    assert g.check(CHAT, 4, BIG).bounce == DEFAULT_BOUNCE


def test_group_repeat_and_unaddressed_after_previous_bounce(tmp_path, clock):
    g = failing_guard(tmp_path, clock)
    # Разносим периодическую уборку и момент первого bounce; проверяем сам gate.
    clock.t += 10
    assert g.check(OTHER, 1, BIG, is_group=True, addressed=True).bounce == DEFAULT_BOUNCE
    assert g.check(OTHER, 2, BIG, is_group=True, addressed=True).bounce is None
    assert g.check(OTHER, 3, BIG, is_group=True, addressed=True).bounce is None
    clock.t = 1120
    g.check(CHAT, 1, BIG)  # уборка; bounce группы возрастом 110 с остаётся
    clock.t = 1130
    assert g.check(OTHER, 4, BIG, is_group=True, addressed=False).bounce is None
    assert g.check(OTHER, 5, BIG, is_group=True, addressed=True).bounce == DEFAULT_BOUNCE


def test_empty_bounce_does_not_touch_state(tmp_path, clock):
    g = failing_guard(tmp_path, clock, bounce_text="")
    for i in range(100):
        assert g.check(CHAT, i, BIG).bounce is None
    assert g._bounced == {}


@pytest.mark.parametrize("mask", [0o022, 0o777])
@pytest.mark.parametrize("existing", [False, True])
def test_day_and_file_modes_and_readability_under_umask(tmp_path, clock, mask, existing):
    root = tmp_path / "root"
    if existing:
        day = root / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day.mkdir(parents=True, mode=0o700)
    g = BurstGuard(root, budget_chars=0, clock=clock)
    previous = os.umask(mask)
    try:
        d = g.check(CHAT, 1, "секрет")
    finally:
        os.umask(previous)
    assert d.action == FOLD
    path = Path(d.path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == "секрет".encode("utf-8")


def test_day_symlink_does_not_write_outside(tmp_path, clock):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    root = tmp_path / "root"
    root.mkdir()
    (root / datetime.now(timezone.utc).strftime("%Y-%m-%d")).symlink_to(outside)
    g = BurstGuard(root, budget_chars=0, clock=clock)
    d = g.check(CHAT, 1, BIG)
    assert d.action == HOLD and d.path is None
    assert list(outside.iterdir()) == []


def test_operator_root_symlink_is_allowed(tmp_path, clock):
    outside = tmp_path / "operator"
    outside.mkdir()
    root = tmp_path / "root"
    root.symlink_to(outside)
    d = BurstGuard(root, budget_chars=0, clock=clock).check(CHAT, 1, BIG)
    assert d.action == FOLD
    assert Path(d.path).resolve().is_relative_to(outside)


@pytest.mark.parametrize("mode", [0o777, 0o720, 0o702])
def test_untrusted_day_permissions_rejected(tmp_path, clock, mode):
    day = tmp_path / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day.mkdir()
    day.chmod(mode)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, BIG)
    assert d.action == HOLD and d.path is None
    assert list(day.iterdir()) == []


def test_wrong_day_owner_rejected(tmp_path, clock, monkeypatch):
    day = tmp_path / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day.mkdir(mode=0o700)
    real_fstat = os.fstat
    def wrong_owner(fd):
        s = real_fstat(fd)
        return SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=s.st_mode)
    monkeypatch.setattr(bg.os, "fstat", wrong_owner)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, BIG)
    assert d.action == HOLD and d.path is None
    assert list(day.iterdir()) == []


def test_surrogates_always_return_decision(tmp_path, clock):
    text = "start\ud800tail\udfff"
    g = default_guard(tmp_path, clock, budget_chars=0)
    d = g.check(CHAT, 1, text)
    assert isinstance(d, Decision) and d.action == FOLD
    assert Path(d.path).read_bytes() == text.encode("utf-8", "backslashreplace")


@pytest.mark.parametrize("kw", [
    {"window_s": 0}, {"window_s": -1}, {"window_s": "120"},
    {"window_s": float("nan")}, {"window_s": float("inf")}, {"window_s": True},
    {"budget_chars": -1}, {"budget_chars": "0"}, {"budget_chars": 1.5},
    {"preview_chars": -1}, {"preview_chars": None}, {"preview_chars": 1.5},
    {"overflow_chars": -1}, {"overflow_chars": 0}, {"overflow_chars": "6000"},
    {"overflow_chars": 1.5}, {"bounce_text": None}, {"bounce_text": 42},
])
def test_bad_parameters_raise_valueerror(tmp_path, kw):
    with pytest.raises(ValueError):
        BurstGuard(tmp_path, **kw)


def test_minimum_overflow_uses_longest_mute_notice(tmp_path, clock):
    g = default_guard(tmp_path, clock)
    minimum = len(g._mute_notice("x" * 64))
    assert g.min_overflow_chars == minimum
    with pytest.raises(ValueError):
        default_guard(tmp_path, clock, overflow_chars=minimum - 1)
    narrow = default_guard(tmp_path, clock, budget_chars=0, overflow_chars=minimum)
    d = narrow.check("x" * 1000, 1, BIG)
    assert d.action == MUTE and len(window_text(d)) == minimum


def test_ids_are_bounded_and_leaf_symlink_safe(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=0)
    d = g.check("7" * 10000, "8" * 10000, BIG)
    assert d.action == FOLD
    assert Path(d.path).name == "7" * 64 + "-" + "8" * 64 + ".txt"
    victim = tmp_path / "victim"
    victim.write_text("KEEP")
    (Path(d.path).parent / "1-1.txt").symlink_to(victim)
    b = g.check(1, 1, BIG)
    assert Path(b.path).name == "1-1-1.txt" and victim.read_text() == "KEEP"


def test_normalized_collisions_never_overwrite(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=0, overflow_chars=100_000)
    ds = [g.check("a/b" if i % 2 else "a.b", 1, "edit-" + str(i)) for i in range(101)]
    assert all(d.action == FOLD for d in ds[:100])
    assert ds[-1].action == HOLD and ds[-1].bounce == DEFAULT_BOUNCE
    assert all(Path(d.path).read_text() == "edit-" + str(i) for i, d in enumerate(ds[:100]))


def test_cleanup_expired_chats_bounces_and_mutes(tmp_path, clock):
    # v4: разрешённое начало; минимальная надбавка по-прежнему вынуждает MUTE.
    minimum = BurstGuard(tmp_path / "blocked" / "bad").min_overflow_chars
    g = failing_guard(tmp_path, clock, preview_chars=4096, overflow_chars=minimum)
    for i in range(1000):
        assert g.check(i, 1, "x" * 10000).action == MUTE
    assert len(g._sent) == len(g._bounced) == len(g._muted) == 1000
    clock.t += g.window_s + 1
    g.check("new", 1, "x")
    assert len(g._sent) <= 2 and len(g._totals) <= 2
    assert len(g._bounced) <= 2 and len(g._muted) <= 2


def test_mute_reserve_is_required_even_when_fold_itself_fits(tmp_path, clock):
    sizing = default_guard(tmp_path, clock, budget_chars=0)
    d = sizing.check(CHAT, 1, BIG)
    cost = len(window_text(d))
    reserve = len(sizing._mute_notice(CHAT))
    allowance = max(sizing.min_overflow_chars, cost + reserve - 1)
    g = default_guard(tmp_path, clock, budget_chars=0, overflow_chars=allowance)
    # Следующий filename с суффиксом только длиннее; его выдача влезла бы без резерва.
    d = g.check(CHAT, 1, BIG)
    assert d.action == MUTE and d.text == "" and d.notice
    assert len(window_text(d)) <= allowance


def test_mute_stays_silent_and_expires_exactly(tmp_path, clock):
    sizing = default_guard(tmp_path, clock)
    g = default_guard(tmp_path, clock, budget_chars=0, overflow_chars=sizing.min_overflow_chars)
    first = g.check(CHAT, 1, BIG)
    assert first.action == MUTE and first.notice and first.path
    for i in range(2, 25):
        d = g.check(CHAT, i, "")
        assert d.action == MUTE and d.text == "" and d.notice is None
        assert window_text(d) == ""
    assert g._used(CHAT, clock.t) == len(first.notice)
    clock.t += g.window_s - 0.001
    assert g.check(CHAT, 30, BIG).notice is None
    clock.t += 0.001
    assert g.check(CHAT, 31, BIG).notice


def test_mute_without_file_can_bounce(tmp_path, clock):
    sizing = failing_guard(tmp_path, clock)
    g = BurstGuard(sizing.fold_dir, clock=clock, budget_chars=0,
                   overflow_chars=sizing.min_overflow_chars)
    d = g.check(CHAT, 1, BIG)
    assert d.action == MUTE and d.path is None and d.bounce == DEFAULT_BOUNCE
    assert g.check(CHAT, 2, BIG).bounce is None


@pytest.mark.parametrize("exc", [OSError("disk"), ValueError("bad path")])
def test_any_save_oserror_or_valueerror_returns_hold_or_mute(tmp_path, clock, monkeypatch, exc):
    g = default_guard(tmp_path, clock, budget_chars=0)
    def fail(*args):
        raise exc
    monkeypatch.setattr(g, "_save", fail)
    d = g.check(CHAT, 1, BIG)
    assert d.action == HOLD and d.path is None and d.bounce == DEFAULT_BOUNCE
    g2 = default_guard(tmp_path, clock, budget_chars=0, overflow_chars=g.min_overflow_chars)
    monkeypatch.setattr(g2, "_save", fail)
    assert g2.check(CHAT, 1, BIG).action == MUTE


@pytest.mark.parametrize("stage", ["open", "write", "close"])
def test_late_save_failure_never_claims_a_full_file(tmp_path, clock, monkeypatch, stage):
    g = default_guard(tmp_path, clock, budget_chars=0)
    original_open, original_fdopen = os.open, os.fdopen
    if stage == "open":
        def fail_open(path, flags, *args, **kw):
            if flags & os.O_CREAT:
                raise OSError("injected open failure")
            return original_open(path, flags, *args, **kw)
        monkeypatch.setattr(bg.os, "open", fail_open)
    else:
        class FailingFile:
            def __init__(self, fd):
                self.file = original_fdopen(fd, "wb")
            def __enter__(self):
                return self
            def write(self, data):
                if stage == "write":
                    self.file.write(data[:3])
                    raise OSError("injected write failure")
                return self.file.write(data)
            def __exit__(self, *args):
                self.file.close()
                if stage == "close":
                    raise ValueError("injected close failure")
        monkeypatch.setattr(bg.os, "fdopen", lambda fd, mode: FailingFile(fd))
    d = g.check(CHAT, 1, BIG)
    assert d.action == HOLD and d.path is None and d.bounce == DEFAULT_BOUNCE
    assert list(g.fold_dir.rglob("*.txt")) == []


def test_two_threads_one_message_budget_with_barriers(tmp_path, clock):
    g = default_guard(tmp_path, clock, budget_chars=4000)
    start = Barrier(2)
    read = Barrier(2)
    original_used = g._used
    def used(chat, now):
        n = original_used(chat, now)
        try:
            read.wait(timeout=0.2)
        except BrokenBarrierError:
            pass
        return n
    g._used = used
    def check(i):
        start.wait(timeout=5)
        return g.check(CHAT, i, BIG)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ds = list(pool.map(check, range(2)))
    assert sum(d.action == DELIVER for d in ds) == 1
    assert g._used(CHAT, clock.t) == sum(len(window_text(d)) for d in ds)


# v3: выходные окна, публикация и пять свидетелей независимого ревью.
def test_repeated_mute_keeps_every_file_and_path(tmp_path, clock):
    g = BurstGuard(tmp_path, clock=clock, budget_chars=0,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    first = g.check(CHAT, 1, "FIRST")
    second = g.check(CHAT, 2, "UNIQUE_SECOND")
    assert first.action == second.action == MUTE
    assert first.notice and second.notice is None
    assert first.path and second.path and first.path != second.path
    assert Path(second.path).read_bytes() == b"UNIQUE_SECOND"
    assert len(list(tmp_path.rglob("*.txt"))) == 2


def test_crlf_cr_nul_are_preserved(tmp_path, clock):
    payload = "a\r\nb\x00\r🙂e\u0301\n"
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, payload)
    assert d.path and Path(d.path).read_bytes() == payload.encode("utf-8")


def test_bootstrap_symlink_race_does_not_chmod_target(tmp_path, clock, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    day = root / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    real_open = os.open
    switched = False
    def race(name, flags, *args, **kw):
        nonlocal switched
        if flags & os.O_PATH and not switched:
            day.rename(root / "moved")
            day.symlink_to(outside)
            switched = True
        return real_open(name, flags, *args, **kw)
    monkeypatch.setattr(bg.os, "open", race)
    d = BurstGuard(root, budget_chars=0, clock=clock).check(CHAT, 1, BIG)
    assert switched and d.path is None and d.action == HOLD
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    assert list(outside.iterdir()) == []


def test_first_failure_after_saved_mute_bounces(tmp_path, clock, monkeypatch):
    g = BurstGuard(tmp_path, clock=clock, budget_chars=0,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    first = g.check(CHAT, 1, "FIRST")
    assert first.path and first.bounce is None and first.notice
    def fail(*args):
        raise OSError("later disk failure")
    monkeypatch.setattr(g, "_save", fail)
    second = g.check(CHAT, 2, "SECOND")
    assert second.action == MUTE and second.path is None and second.notice is None
    assert second.bounce == DEFAULT_BOUNCE
    assert g.check(CHAT, 3, "THIRD").bounce is None


@pytest.mark.parametrize("failed", [False, True])
def test_delayed_write_uses_return_window_for_notice_and_bounce(tmp_path, clock, monkeypatch, failed):
    g = BurstGuard(tmp_path, clock=clock, budget_chars=0,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    real_fdopen = os.fdopen
    first = True
    class SlowFile:
        def __init__(self, fd):
            self.f = real_fdopen(fd, "wb")
        def __enter__(self):
            return self
        def write(self, data):
            nonlocal first
            if first:
                first = False
                clock.t += g.window_s + 1  # время двигает запись, не check
            if failed:
                raise OSError("delayed ENOSPC")
            return self.f.write(data)
        def __exit__(self, *args):
            self.f.close()
    monkeypatch.setattr(bg.os, "fdopen", lambda fd, mode: SlowFile(fd))
    history = []
    ds = []
    for i in range(2):
        d = g.check("x" * 64, i, "PAYLOAD")
        ds.append(d)
        history.append((clock(), len(window_text(d))))  # момент возврата
        actual = sum(n for t, n in history if clock() - t < g.window_s)
        assert actual <= g.budget_chars + g.overflow_chars
        assert g._used("x" * 64, clock()) == actual
        if not failed:
            assert d.path and Path(d.path).read_bytes() == b"PAYLOAD"
    assert sum(bool(d.notice) for d in ds) == 1
    assert sum(bool(d.bounce) for d in ds) == int(failed)
    assert g._muted["x" * 64] == history[0][0]
    if failed:
        assert g._bounced["x" * 64] == history[0][0]


@pytest.mark.parametrize("initial_mute,failed", [(False, False), (True, False), (True, True)])
def test_decision_is_reconsidered_after_io(tmp_path, clock, monkeypatch, initial_mute, failed):
    g = BurstGuard(tmp_path, budget_chars=10, clock=clock,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    first = g.check(CHAT, 1, "x" * (11 if initial_mute else 10))
    assert first.action == (MUTE if initial_mute else DELIVER)
    real_fdopen = os.fdopen
    class DelayedFile:
        def __init__(self, fd):
            self.f = real_fdopen(fd, "wb")
        def __enter__(self):
            return self
        def write(self, data):
            clock.t += 121
            if failed:
                raise OSError("late failure")
            return self.f.write(data)
        def __exit__(self, *args):
            self.f.close()
    monkeypatch.setattr(bg.os, "fdopen", lambda fd, mode: DelayedFile(fd))
    d = g.check(CHAT, 2, "fresh")
    assert d.action == DELIVER and d.text == "fresh" and d.bounce is None
    assert g._used(CHAT, clock()) == len("fresh")
    assert CHAT not in g._muted and CHAT not in g._bounced
    if not failed:
        assert d.path and Path(d.path).read_bytes() == b"fresh"


def test_bounce_and_mute_expire_during_failed_write(tmp_path, clock, monkeypatch):
    g = BurstGuard(tmp_path, budget_chars=0, clock=clock,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    real_fdopen = os.fdopen
    class FailedFile:
        def __init__(self, fd):
            self.f = real_fdopen(fd, "wb")
        def __enter__(self):
            return self
        def write(self, data):
            clock.t += 121
            raise OSError("late failure")
        def __exit__(self, *args):
            self.f.close()
    monkeypatch.setattr(bg.os, "fdopen", lambda fd, mode: FailedFile(fd))
    a = g.check(CHAT, 1, "FIRST")
    b = g.check(CHAT, 2, "SECOND")
    assert a.notice and b.notice and a.bounce and b.bounce
    assert g._muted[CHAT] == g._bounced[CHAT] == clock()
    assert g._used(CHAT, clock()) == len(window_text(b))


def test_directory_close_after_publication_keeps_path(tmp_path, clock, monkeypatch):
    day = tmp_path / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day.mkdir(mode=0o700)  # bootstrap уже не нужен
    real_close = os.close
    failures = []
    def close(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if directory:
            failures.append(fd)
            clock.t += 121
            raise OSError("directory close after publication")
    monkeypatch.setattr(bg.os, "close", close)
    g = BurstGuard(tmp_path, budget_chars=0, clock=clock)
    d = g.check(CHAT, 1, "FULL CONTENT")
    assert len(failures) == 2
    assert d.action == FOLD and d.path and d.bounce is None
    assert Path(d.path).read_bytes() == b"FULL CONTENT"
    assert g._sent[CHAT][-1][0] == clock()


@pytest.mark.parametrize("deny_unlink", [False, True])
def test_partial_write_never_has_published_name(tmp_path, clock, monkeypatch, deny_unlink):
    real_fdopen = os.fdopen
    real_unlink = os.unlink
    class PartialFile:
        def __init__(self, fd):
            self.f = real_fdopen(fd, "wb")
        def __enter__(self):
            return self
        def write(self, data):
            self.f.write(data[:7])
            self.f.flush()
            raise OSError("ENOSPC after partial write")
        def __exit__(self, *args):
            self.f.close()
    def unlink(*args, **kw):
        if deny_unlink:
            raise PermissionError("unlink forbidden")
        return real_unlink(*args, **kw)
    monkeypatch.setattr(bg.os, "fdopen", lambda fd, mode: PartialFile(fd))
    monkeypatch.setattr(bg.os, "unlink", unlink)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, "COMPLETE TEXT")
    assert d.action == HOLD and d.path is None
    assert list(tmp_path.rglob("*.txt")) == []
    parts = list(tmp_path.rglob(".part-*"))
    assert len(parts) == int(deny_unlink)
    if parts:
        assert parts[0].read_bytes() == b"COMPLET"


def test_publication_is_exclusive_and_follows_complete_close(tmp_path, clock, monkeypatch):
    real_link = os.link
    calls = []
    day = tmp_path / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day.mkdir(mode=0o700)
    old = day / (CHAT + "-1.txt")
    old.write_bytes(b"OLD")
    def link(src, dst, **kw):
        assert str(src).startswith(".part-") and not str(src).endswith(".txt")
        assert kw["src_dir_fd"] == kw["dst_dir_fd"] and kw["follow_symlinks"] is False
        source = Path(os.readlink('/proc/self/fd/' + str(kw['src_dir_fd']))) / src
        assert source.read_bytes() == b"FULL CONTENT"
        calls.append(dst)
        return real_link(src, dst, **kw)
    monkeypatch.setattr(bg.os, "link", link)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, "FULL CONTENT")
    assert calls == [CHAT + "-1.txt", CHAT + "-1-1.txt"]
    assert old.read_bytes() == b"OLD"
    assert d.path and Path(d.path).read_bytes() == b"FULL CONTENT"
    assert list(day.glob(".part-*")) == []


def test_publication_failure_never_returns_path(tmp_path, clock, monkeypatch):
    def fail(*args, **kw):
        raise OSError("link unavailable")
    monkeypatch.setattr(bg.os, "link", fail)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, "FULL CONTENT")
    assert d.action == HOLD and d.path is None
    assert list(tmp_path.rglob("*.txt")) == []
    assert list(tmp_path.rglob(".part-*")) == []


def test_midnight_notice_finds_both_days_and_mentions_failure(tmp_path, clock, monkeypatch):
    from datetime import timedelta
    import glob
    class Wall:
        current = datetime(2026, 10, 2, 23, 59, 59, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(bg, "datetime", Wall)
    g = BurstGuard(tmp_path, budget_chars=0, clock=clock,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    a = g.check(CHAT, 1, "BEFORE")
    Wall.current += timedelta(seconds=2)
    clock.t += 2
    b = g.check(CHAT, 2, "AFTER")
    pattern = str(tmp_path / "*" / (CHAT + "-*.txt"))
    assert a.notice and pattern in a.notice and b.notice is None
    assert "если запись удалась; при сбое текст теряется" in a.notice
    assert set(glob.glob(pattern)) == {a.path, b.path}
    assert Path(a.path).read_bytes() == b"BEFORE" and Path(b.path).read_bytes() == b"AFTER"
    assert len(window_text(a)) + len(window_text(b)) <= g.overflow_chars


def test_failed_mute_notice_is_truthful(tmp_path, clock, monkeypatch):
    g = BurstGuard(tmp_path, budget_chars=0, clock=clock, bounce_text="",
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    def fail(*args):
        raise OSError("no disk")
    monkeypatch.setattr(g, "_save", fail)
    d = g.check("x" * 64, 1, "LOST")
    assert d.action == MUTE and d.path is None and d.bounce is None
    assert "если запись удалась; при сбое текст теряется" in d.notice
    assert len(d.notice) == g.min_overflow_chars


def test_short_write_is_not_published(tmp_path, clock, monkeypatch):
    real_fdopen = os.fdopen
    class ShortFile:
        def __init__(self, fd):
            self.f = real_fdopen(fd, "wb")
        def __enter__(self):
            return self
        def write(self, data):
            return self.f.write(data[:3])
        def __exit__(self, *args):
            self.f.close()
    monkeypatch.setattr(bg.os, "fdopen", lambda fd, mode: ShortFile(fd))
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, "COMPLETE TEXT")
    assert d.action == HOLD and d.path is None
    assert list(tmp_path.rglob("*.txt")) == []
    assert list(tmp_path.rglob(".part-*")) == []


def test_full_path_survives_denied_temporary_cleanup(tmp_path, clock, monkeypatch):
    def unlink(*args, **kw):
        raise PermissionError("cleanup forbidden")
    monkeypatch.setattr(bg.os, "unlink", unlink)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, "FULL CONTENT")
    assert d.action == FOLD and d.path and d.bounce is None
    assert Path(d.path).read_bytes() == b"FULL CONTENT"
    parts = list(tmp_path.rglob(".part-*"))
    assert len(parts) == 1 and parts[0].read_bytes() == b"FULL CONTENT"


# v4: момент решения, параметры, glob и свидетели N01–N03.
@pytest.mark.parametrize("kw,message", [
    ({"window_s": 0.008}, "window_s.*1.0"),
    ({"window_s": 0.999999}, "window_s.*1.0"),
    ({"preview_chars": 4097}, "preview_chars.*4096"),
    ({"preview_chars": 32_000_000}, "preview_chars.*4096"),
])
def test_v4_parameter_limits_are_explicit(tmp_path, kw, message):
    with pytest.raises(ValueError, match=message):
        BurstGuard(tmp_path, **kw)


def test_temporary_collisions_do_not_remove_unowned_file(tmp_path, clock, monkeypatch):
    day = tmp_path / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day.mkdir(mode=0o700)
    other = day / ".part-fixed"
    other.write_bytes(b"PREEXISTING")
    attempts = []
    def uuid():
        attempts.append(1)
        return SimpleNamespace(hex="fixed")
    monkeypatch.setattr(bg.uuid, "uuid4", uuid)
    d = BurstGuard(tmp_path, budget_chars=0, clock=clock).check(CHAT, 1, "NEW")
    assert len(attempts) == 100
    assert d.action == HOLD and d.path is None
    assert other.exists() and other.read_bytes() == b"PREEXISTING"
    assert set(day.iterdir()) == {other}


@pytest.mark.parametrize("first_addressed", [False, True])
def test_muted_group_failure_without_address_never_bounces(tmp_path, clock, monkeypatch, first_addressed):
    g = BurstGuard(tmp_path, budget_chars=0, clock=clock,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    a = g.check(CHAT, 1, "FIRST", is_group=True, addressed=first_addressed)
    assert a.action == MUTE and a.path and a.bounce is None
    def fail(*args):
        raise OSError("first failure after saved MUTE")
    monkeypatch.setattr(g, "_save", fail)
    b = g.check(CHAT, 2, "SECOND", is_group=True, addressed=False)
    assert b.action == MUTE and b.path is None and b.notice is None and b.bounce is None
    assert CHAT not in g._bounced
    addressed = g.check(CHAT, 3, "THIRD", is_group=True, addressed=True)
    assert addressed.bounce == DEFAULT_BOUNCE


def test_early_failure_does_not_leak_root_fd(tmp_path, clock):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "folds"
    root.mkdir()
    (root / datetime.now(timezone.utc).strftime("%Y-%m-%d")).symlink_to(outside)
    g = BurstGuard(root, budget_chars=0, clock=clock)
    before = len(os.listdir('/proc/self/fd'))
    for i in range(8):
        d = g.check(CHAT, i, "NEW")
        assert d.path is None and d.action == HOLD
        assert len(os.listdir('/proc/self/fd')) == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("directory", ["folds[prod]", "folds*prod?", "[root]/nested[*?]"])
def test_mute_glob_escapes_literal_path_and_reserves_all_chars(tmp_path, clock, directory):
    import glob
    root = tmp_path / directory
    minimum = BurstGuard(root).min_overflow_chars
    g = BurstGuard(root, budget_chars=0, overflow_chars=minimum, clock=clock)
    d = g.check("x" * 64, 1, "COMPLETE TEXT")
    pattern = str(Path(glob.escape(str(root))) / "*" / ("x" * 64 + "-*.txt"))
    assert d.action == MUTE and d.path and pattern in d.notice
    assert glob.glob(pattern) == [d.path]
    assert Path(d.path).read_bytes() == b"COMPLETE TEXT"
    assert len(d.notice) == minimum and g._used("x" * 64, clock()) == minimum
    with pytest.raises(ValueError):
        BurstGuard(root, overflow_chars=minimum - 1)


def test_minimal_cpu_at_allowed_boundaries(tmp_path):
    # Два публичных check, настоящие часы и файл 64 MB, как у Астры.
    import time
    g = BurstGuard(tmp_path, budget_chars=0, preview_chars=4096, window_s=1.0,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    text = "x" * 64_000_000
    a = g.check("x" * 64, 1, text)
    first_decision = g._muted["x" * 64]
    t1 = time.monotonic()
    b = g.check("x" * 64, 2, "small")
    t2 = time.monotonic()
    assert a.path and Path(a.path).read_bytes() == text.encode()
    assert b.path and Path(b.path).read_bytes() == b"small"
    assert a.action == b.action == MUTE and a.notice
    # Возвраты измерены, но договор относится к моментам решения, не к return_gap.
    if b.notice:
        assert g._muted["x" * 64] - first_decision >= g.window_s
    else:
        assert b.notice is None
        assert len(window_text(a)) + len(window_text(b)) <= g.overflow_chars


@pytest.mark.parametrize("failed", [False, True])
def test_preview_and_notices_are_ready_before_decision_clock(tmp_path, clock, monkeypatch, failed):
    g = BurstGuard(tmp_path, budget_chars=0, preview_chars=4096, window_s=1.0,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars, clock=clock)
    original_preview, original_notice = g._preview, g._mute_notice
    events = []
    def slow_preview(text):
        events.append("preview")
        clock.t += 2
        return original_preview(text)
    def slow_notice(chat):
        events.append("mute_notice")
        clock.t += 2
        return original_notice(chat)
    def read_clock():
        events.append("clock")
        return clock()
    monkeypatch.setattr(g, "_preview", slow_preview)
    monkeypatch.setattr(g, "_mute_notice", slow_notice)
    monkeypatch.setattr(g, "_clock", read_clock)
    if failed:
        def fail(*args):
            raise OSError("disk full")
        monkeypatch.setattr(g, "_save", fail)
    d = g.check("x" * 64, 1, "x" * 1_000_000)
    assert d.action == MUTE and d.notice
    assert events[-1] == "clock"
    assert events.index("preview") < len(events) - 1
    assert events.index("mute_notice") < len(events) - 1
    assert g._muted["x" * 64] == clock()
    assert g._sent["x" * 64][-1][0] == clock()
    assert g._used("x" * 64, clock()) == len(window_text(d))
    if failed:
        assert d.bounce and g._bounced["x" * 64] == clock()


def test_hold_prepares_bounce_text_before_gate_expires(tmp_path, clock, monkeypatch):
    g = BurstGuard(tmp_path, budget_chars=0, window_s=1.0, clock=clock)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(g, "_save", fail)
    first = g.check(CHAT, 1, "payload")
    assert first.action == HOLD and first.bounce
    assert "отправителю предложено" in first.notice
    second = g.check(CHAT, 2, "payload")
    assert second.action == HOLD and second.bounce is None
    assert "отправителю ничего не отправлено" in second.notice
    original_preview = g._preview
    def slow_preview(text):
        clock.t += 2
        return original_preview(text)
    monkeypatch.setattr(g, "_preview", slow_preview)
    third = g.check(CHAT, 3, "payload")
    assert third.action == HOLD and third.bounce
    assert "отправителю предложено" in third.notice
    assert g._used(CHAT, clock()) == len(window_text(third))
    assert g._bounced[CHAT] == clock()


def test_global_cleanup_finishes_before_final_clock(tmp_path, clock, monkeypatch):
    g = BurstGuard(tmp_path, budget_chars=0, clock=clock,
                   overflow_chars=BurstGuard(tmp_path).min_overflow_chars)
    events = []
    original_cleanup = g._cleanup
    def cleanup(now):
        events.append("cleanup")
        original_cleanup(now)
        clock.t += 121
    def read_clock():
        events.append("clock")
        return clock()
    monkeypatch.setattr(g, "_cleanup", cleanup)
    monkeypatch.setattr(g, "_clock", read_clock)
    d = g.check(CHAT, 1, "payload")
    assert d.action == MUTE and events[-1] == "clock"
    assert g._muted[CHAT] == clock()
    assert g._sent[CHAT][-1][0] == clock()


def test_fast_deliver_has_final_clock_after_cleanup(tmp_path, clock, monkeypatch):
    g = BurstGuard(tmp_path, budget_chars=10, clock=clock)
    cleanup = g._cleanup
    def slow_cleanup(now):
        cleanup(now)
        clock.t += 121
    monkeypatch.setattr(g, "_cleanup", slow_cleanup)
    d = g.check(CHAT, 1, "small")
    assert d.action == DELIVER and g._sent[CHAT][-1][0] == clock()
