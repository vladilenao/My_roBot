"""Исполнитель публикует типизированные события, а не готовые фразы.

Русский текст уведомления имеет право собирать только шаблон канала. Тест
проверяет это по исходнику брокера: каждое место публикации обязано называть
значение типа из каталога и не содержать ни одного строкового литерала с
кириллицей.

Обследование change нашло тринадцать мест эмиссии, но строки прежнего кода
1022 и 1024 описывали один и тот же факт закрытия: второе место передавало
``reason`` вместо типа, поэтому уведомление о закрытии приходилось дважды.
Места слиты, и ``EXPECTED_EMISSIONS`` фиксирует результат: новое место
эмиссии без разбораchange не появится молча.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_PATH = Path(__file__).resolve().parents[3] / "src" / "broker" / "journal_broker.py"
PUBLISH_METHOD = "_publish_event"
EXPECTED_EMISSIONS = 12
_CYRILLIC = range(0x0400, 0x0500)


def _has_cyrillic(value: str) -> bool:
    return any(ord(char) in _CYRILLIC for char in value)


def _publish_calls(tree: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == PUBLISH_METHOD:
            calls.append(node)
    return calls


def _literals(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _emission_sites() -> list[ast.Call]:
    calls = _publish_calls(ast.parse(SOURCE_PATH.read_text(encoding="utf-8")))
    assert len(calls) == EXPECTED_EMISSIONS, (
        f"мест публикации событий: {len(calls)}, ожидалось {EXPECTED_EMISSIONS}"
    )
    return calls


def test_every_emission_site_uses_catalogue_type() -> None:
    for call in _emission_sites():
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Attribute), (
            f"тип события передан не значением каталога: {ast.dump(first) if first else 'нет аргумента'}"
        )
        assert isinstance(first.value, ast.Name) and first.value.id == "EventType", (
            f"тип события {first.attr} взят не из каталога EventType"
        )
        assert first.attr.isupper(), f"имя значения каталога {first.attr} не в верхнем регистре"


def test_emission_sites_contain_no_russian_phrases() -> None:
    offenders = sorted(
        literal
        for call in _emission_sites()
        for literal in _literals(call)
        if _has_cyrillic(literal)
    )

    assert not offenders, (
        "исполнитель собирает текст уведомления вместо структурных полей: "
        f"{offenders}"
    )
