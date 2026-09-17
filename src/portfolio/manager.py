import math
from typing import Iterable, Mapping

from src.portfolio.account import Account
from src.portfolio.models import ContractMeta, PendingOrder, Position, Signal, SizingOutcome


class PositionManager:
    """Transitional in-memory portfolio view for the legacy CSV simulator."""

    def __init__(self, initial_deposit: float, max_risk_pct: float, *, realized: float = 0.0,
                 positions: Iterable[Position] = (), pending: Iterable[PendingOrder] = ()):
        self.account = Account(initial_deposit)
        self.account.realize(realized)
        self.max_risk_pct = float(max_risk_pct)
        self.positions = {position.position_id: position for position in positions}
        self.pending = {order.order_id: order for order in pending}
        self._prices: dict[str, float] = {}
        self._contracts: dict[str, ContractMeta] = {}

    def budget(self) -> float:
        base = min(self.account.balance, self.account.equity)
        return base * self.max_risk_pct / 100.0

    def has_open_for(self, ticker: str) -> bool:
        return any(p.ticker == ticker and p.qty > 0 for p in self.positions.values())

    def get_go(self, qty: int, contract: ContractMeta, side: str) -> float:
        side_go = contract.go_buy if side == "BUY" else contract.go_sell
        return side_go * qty

    def evaluate_signals(self, signals: list[Signal], contracts: dict[str, ContractMeta] | None = None) -> list[SizingOutcome]:
        contracts = contracts or {}
        outcomes: list[SizingOutcome] = []
        for signal in signals:
            contract = contracts.get(signal.ticker)
            outcomes.append(self._size_signal(signal, contract, contracts) if contract is not None else SizingOutcome(
                signal=signal, qty=0, risk_rub=0.0, risk_pct=signal.risk_pct, reason="no-contract-meta",
            ))
        return outcomes

    def _size_signal(self, signal: Signal, contract: ContractMeta,
                     contracts: Mapping[str, ContractMeta] | None = None) -> SizingOutcome:
        def reject(reason: str) -> SizingOutcome:
            return SizingOutcome(signal=signal, qty=0, risk_rub=0.0, risk_pct=signal.risk_pct, reason=reason)

        if self.has_open_for(signal.ticker):
            return reject("otherexisting")
        if not self._has_stop_info(signal):
            return reject("stop-missing")
        used = min(signal.risk_pct, self.max_risk_pct)
        if used <= 0:
            return reject("risk-pct-exceeded")
        risk_per_contract = self._risk_per_contract(signal, contract)
        if risk_per_contract is None:
            return reject("no-margin-math")
        cap = min(self.account.balance, self.account.equity) * used / 100.0
        qty = math.floor(cap / risk_per_contract)
        if qty < 1:
            return reject("qty-negative")
        actual_risk = round(qty * risk_per_contract, 6)
        if self._open_risk(contracts) + actual_risk > self.budget():
            return reject("aggregate-overflow")
        return SizingOutcome(signal=signal, qty=int(qty), risk_rub=actual_risk, risk_pct=used)

    @staticmethod
    def _has_stop_info(signal: Signal) -> bool:
        if signal.take_profit is not None and signal.stop_price is not None:
            return True
        return signal.stop_distance_pct is not None and signal.stop_distance_pct > 0

    @staticmethod
    def _risk_per_contract(signal: Signal, contract: ContractMeta) -> float | None:
        """Фактический риск на контракт: геометрия полного тейка, иначе стоп-дистанция."""
        if contract.price_step <= 0 or contract.step_cost <= 0:
            return None

        def distance_to_rub(distance: float) -> float:
            return distance / contract.price_step * contract.step_cost

        if signal.take_profit is not None and signal.stop_price is not None:
            distance = abs(signal.take_profit - signal.stop_price)
            if distance <= 0:
                return None
            return distance_to_rub(distance)
        stop_distance = (
            (signal.stop_distance_pct / 100.0) * signal.entry_price if signal.stop_distance_pct else None
        )
        if stop_distance is None or stop_distance <= 0:
            return None
        return distance_to_rub(stop_distance)

    def _open_risk(self, contracts: Mapping[str, ContractMeta] | None = None) -> float:
        """Суммарный фактический риск по открытым позициям (геометрия тейка/стопа)."""
        contracts = contracts or {}
        total = 0.0
        for pos in self.positions.values():
            if pos.qty <= 0:
                continue
            contract = contracts.get(pos.ticker) or self._contracts.get(pos.ticker)
            if contract is None or contract.price_step <= 0 or contract.step_cost <= 0:
                continue
            if pos.take_profit is not None and pos.stop_price is not None:
                distance = abs(pos.take_profit - pos.stop_price)
            elif pos.stop_price is not None:
                distance = abs(pos.avg_price - pos.stop_price)
            else:
                continue
            if distance <= 0:
                continue
            total += distance / contract.price_step * contract.step_cost * pos.qty
        return total

    def margin_ok(self, qty: int, contract: ContractMeta, side: str) -> bool:
        return self.account.balance >= self.get_go(qty, contract, side)

    def over_risk_cap(self) -> float:
        return 3.0 * self.account.initial_deposit

    def apply_over_risk(self, positions: dict[str, Position]) -> list[str]:
        flagged = []
        for pos in positions.values():
            if pos.qty <= 0 or pos.ticker not in self._prices:
                continue
            contract = self._contracts.get(pos.ticker)
            if contract is None:
                continue
            if contract.position_value(pos.qty, self._prices[pos.ticker]) > self.over_risk_cap():
                pos.mark_over_risk()
                flagged.append(pos.position_id)
        return flagged

    def track_bar(self, prices: dict[str, float], contracts: dict[str, ContractMeta] | None = None) -> list[str]:
        self._prices = dict(prices)
        self._contracts = dict(contracts or {})
        over_ids = set(self.apply_over_risk(self.positions))
        if not over_ids:
            return []
        cancel_candidates = sorted(
            (order for order in self.pending.values() if order.position_id not in over_ids),
            key=lambda order: (order.ts_order, order.order_id),
        )
        return [cancel_candidates[0].position_id] if cancel_candidates else []

    def register_order(self, order: PendingOrder) -> None:
        self.pending[order.order_id] = order

    def drop_order(self, order_id: int) -> None:
        self.pending.pop(order_id, None)

    def drop_order_stale(self, position_id: str) -> None:
        self.pending = {oid: order for oid, order in self.pending.items() if order.position_id != position_id}
