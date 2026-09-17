"""Immutable, reproducible records for calculations that affect trading."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from src.trade_journal.storage import Storage


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    return value


@dataclass(frozen=True)
class MeasuredValue:
    """A numeric or categorical value accompanied by its explicit unit."""

    value: object
    unit: str

    def __post_init__(self) -> None:
        if not self.unit:
            raise ValueError("audit value unit is required")


@dataclass(frozen=True)
class MarketInput:
    """An immutable source-data window, including recurrent-calculation seed."""

    source_data_id: str
    kind: str
    values: Mapping[str, MeasuredValue]
    seed: Mapping[str, MeasuredValue]
    available_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.source_data_id or not self.kind:
            raise ValueError("source_data_id and kind are required")
        if not self.values:
            raise ValueError("market input values are required")
        object.__setattr__(self, "values", _freeze(dict(self.values)))
        object.__setattr__(self, "seed", _freeze(dict(self.seed)))


@dataclass(frozen=True)
class FormulaStep:
    formula: str
    operands: Mapping[str, MeasuredValue]
    result: MeasuredValue

    def __post_init__(self) -> None:
        if not self.formula or not self.operands:
            raise ValueError("formula and operands are required")
        object.__setattr__(self, "operands", _freeze(dict(self.operands)))


@dataclass(frozen=True)
class Rounding:
    rule: str
    unrounded: MeasuredValue
    rounded: MeasuredValue

    def __post_init__(self) -> None:
        if not self.rule:
            raise ValueError("rounding rule is required")
        if self.unrounded.unit != self.rounded.unit:
            raise ValueError("rounded values must have the same unit")


class TraceOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class TraceLinks:
    assignment_id: str | None = None
    trade_id: str | None = None
    signal_id: str | None = None
    command_id: str | None = None
    event_id: str | None = None
    service_uid: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class CalculationTrace:
    calculation_id: str
    algorithm: str
    algorithm_version: str
    inputs: Mapping[str, MeasuredValue]
    steps: tuple[FormulaStep, ...]
    rounding: tuple[Rounding, ...]
    result: MeasuredValue
    outcome: TraceOutcome
    reason: str
    links: TraceLinks
    source_data_id: str | None = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if not all((self.calculation_id, self.algorithm, self.algorithm_version, self.reason)):
            raise ValueError("calculation ID, algorithm, version, and reason are required")
        if not self.inputs or not self.steps or not self.rounding:
            raise ValueError("inputs, steps, and rounding are required for an audit trace")
        object.__setattr__(self, "inputs", _freeze(dict(self.inputs)))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "rounding", tuple(self.rounding))


class CalculationTraceRepository:
    """Persists immutable market snapshots and their calculation traces together."""

    def __init__(self, storage: "Storage") -> None:
        self._storage = storage

    def record(self, trace: CalculationTrace, market_input: MarketInput | None = None) -> bool:
        if market_input is not None and trace.source_data_id != market_input.source_data_id:
            raise ValueError("trace source_data_id must match market input")
        if trace.source_data_id is not None and market_input is None:
            raise ValueError("a trace with source_data_id requires its market input")
        with self._storage.transaction() as connection:
            if market_input is not None:
                inserted = connection.execute(
                    "INSERT INTO market_inputs (source_data_id, kind, data_json, seed_json, available_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source_data_id) DO NOTHING",
                    (
                        market_input.source_data_id,
                        market_input.kind,
                        _dump(market_input.values),
                        _dump(market_input.seed),
                        market_input.available_at.isoformat(),
                        market_input.created_at.isoformat(),
                    ),
                ).rowcount
                if not inserted:
                    self._verify_market_input(connection, market_input)
            return bool(connection.execute(
                "INSERT INTO calculations (calculation_id, source_data_id, trade_id, command_id, event_id, "
                "assignment_id, signal_id, service_uid, correlation_id, algorithm, algorithm_version, "
                "input_json, steps_json, rounding_json, output_json, outcome, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (calculation_id) DO NOTHING",
                (
                    trace.calculation_id, trace.source_data_id, trace.links.trade_id,
                    trace.links.command_id, trace.links.event_id, trace.links.assignment_id,
                    trace.links.signal_id, trace.links.service_uid, trace.links.correlation_id,
                    trace.algorithm, trace.algorithm_version, _dump(trace.inputs), _dump(trace.steps),
                    _dump(trace.rounding), _dump(trace.result), trace.outcome.value, trace.reason,
                    trace.created_at.isoformat(),
                ),
            ).rowcount)

    @staticmethod
    def _verify_market_input(connection: object, market_input: MarketInput) -> None:
        row = connection.execute(
            "SELECT kind, data_json, seed_json, available_at FROM market_inputs WHERE source_data_id = ?",
            (market_input.source_data_id,),
        ).fetchone()
        expected = (
            market_input.kind, _dump(market_input.values), _dump(market_input.seed),
            market_input.available_at.isoformat(),
        )
        if row != expected:
            raise ValueError("source_data_id already belongs to different immutable market input")


def _dump(value: object) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))


def calculation_trace(
    algorithm: str,
    *,
    inputs: Mapping[str, MeasuredValue],
    result: MeasuredValue,
    reason: str,
    formula: str,
    links: TraceLinks = TraceLinks(),
    outcome: TraceOutcome = TraceOutcome.ACCEPTED,
    source_data_id: str | None = None,
    algorithm_version: str = "1",
    rounding: Rounding | None = None,
) -> CalculationTrace:
    """Build a deterministic, secret-free trace for a pure calculation.

    Callers provide the complete local inputs and units.  The stable ID makes
    retries idempotent while retaining enough data to reproduce the result
    without fetching market data or credentials.
    """
    material = _dump({
        "algorithm": algorithm,
        "version": algorithm_version,
        "inputs": inputs,
        "result": result,
        "reason": reason,
        "formula": formula,
        "links": links,
        "source_data_id": source_data_id,
    })
    calculation_id = hashlib.sha256(material.encode()).hexdigest()
    rounding = rounding or Rounding("no rounding", result, result)
    return CalculationTrace(
        calculation_id=calculation_id,
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        inputs=inputs,
        steps=(FormulaStep(formula, inputs, result),),
        rounding=(rounding,),
        result=result,
        outcome=outcome,
        reason=reason,
        links=links,
        source_data_id=source_data_id,
    )
