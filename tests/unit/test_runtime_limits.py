"""Процесс робота сам поднимает предел открытых файлов (fix-sqlite-connection-close)."""

from __future__ import annotations

import pytest

from src.runtime_limits import OPEN_FILES_TARGET, describe, raise_open_files_limit

INFINITY = -1


class FakeResource:
    RLIMIT_NOFILE = 8
    RLIM_INFINITY = INFINITY

    def __init__(self, soft, hard, fail=None):
        self.limits, self.fail, self.calls = (soft, hard), fail, []

    def getrlimit(self, which):
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which, limits):
        self.calls.append(limits)
        if self.fail is not None:
            raise self.fail
        self.limits = limits


def test_terminal_default_is_raised_to_target():
    fake = FakeResource(256, INFINITY)
    limit = raise_open_files_limit(module=fake)
    assert fake.calls == [(OPEN_FILES_TARGET, INFINITY)]
    assert (limit.before, limit.after, limit.raised) == (256, 8192, True)
    assert describe(limit) == ("info", "Предел открытых файлов поднят: 256 → 8192")


def test_hard_limit_below_target_caps_the_raise():
    fake = FakeResource(256, 4096)
    limit = raise_open_files_limit(module=fake)
    assert fake.calls == [(4096, 4096)]
    assert limit.after == 4096


@pytest.mark.parametrize("soft", [8192, 1_048_576, INFINITY])
def test_high_enough_limit_is_left_alone(soft):
    fake = FakeResource(soft, INFINITY)
    limit = raise_open_files_limit(module=fake)
    assert fake.calls == []
    assert not limit.raised and describe(limit) is None


def test_refused_raise_warns_and_keeps_running():
    fake = FakeResource(256, INFINITY, fail=ValueError("not allowed"))
    limit = raise_open_files_limit(module=fake)
    assert (limit.before, limit.after, limit.error) == (256, 256, "ValueError")
    level, text = describe(limit)
    assert level == "warning" and "осталось 256" in text and "ulimit -n 8192" in text


def test_platform_without_resource_module_is_skipped():
    limit = raise_open_files_limit(module=None)
    assert (limit.before, limit.after, limit.error) == (None, None, None)
    assert describe(limit) is None


def test_main_raises_limit_before_dispatch(monkeypatch):
    import run

    seen = []
    monkeypatch.setattr(run, "raise_open_files_limit", lambda: seen.append("limit") or "итог")
    monkeypatch.setattr(run, "_run_contour", lambda kind, args=(): seen.append(("contour", kind)) or 0)
    assert run.main(["--contour", "emulation"]) == 0
    assert seen == ["limit", ("contour", "emulation")]
    assert run._OPEN_FILES == "итог"
