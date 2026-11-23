"""Запуск команды Финама на фейковом API: среда, часы, вывод и файлы прогона."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.finam.__main__ import FinamEnv, main
from src.finam.checks import Timing
from tests.unit.finam.fakes import ACCOUNT_ID, DEMO_ACCOUNT_ID, SECRET, TINKOFF, FakeFinam, gateway_factory, \
    reference_factory


@dataclass
class Result:
    code: int
    lines: list = field(default_factory=list)
    base: Path | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in (self.base / "finam").iterdir() if path.is_dir())

    def report(self) -> str:
        return (self.run_dirs()[-1] / "report.md").read_text(encoding="utf-8")

    def records(self) -> str:
        return (self.run_dirs()[-1] / "responses.jsonl").read_text(encoding="utf-8")

    def row(self, code: str) -> tuple[str, str]:
        for line in self.report().splitlines():
            if line.startswith(f"| {code} |"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                return cells[2], cells[3]
        raise AssertionError(f"нет строки {code}")

    def all_files_text(self) -> str:
        parts = [self.text]
        for path in (self.base / "finam").rglob("*"):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8"))
        return "\n".join(parts)


def default_env(**changes) -> FinamEnv:
    values = {"secret": SECRET, "account_id": ACCOUNT_ID, "demo_secret": None, "demo_account_id": DEMO_ACCOUNT_ID,
              "tinkoff_token": TINKOFF}
    values.update(changes)
    return FinamEnv(**values)


@pytest.fixture
def broker() -> FakeFinam:
    return FakeFinam()


@pytest.fixture
def run(broker, tmp_path):
    def invoke(*argv, env: FinamEnv | None = None, answer: str = "ДА", reference=True) -> Result:
        lines: list[str] = []
        timing = Timing(clock=broker.clock, sleep=broker.sleep, poll_secs=10, event_wait_secs=5,
                        stream_ready_secs=0.05, transactions_wait_secs=1, minute_secs=60)
        code = main(list(argv), env=env or default_env(), gateway_factory=gateway_factory(broker),
                    reference_factory=reference_factory() if reference else None, out=lines.append,
                    ask=lambda prompt: (lines.append(prompt), answer)[1], base_dir=tmp_path, timing=timing)
        for stream in broker.streams:
            stream.cancel()
        return Result(code, lines, tmp_path)

    return invoke
