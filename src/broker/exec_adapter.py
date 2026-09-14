from datetime import datetime, timezone

from src.broker.journal_broker import JournalBroker
from src.broker.port import BrokerPort
from src.config import MAX_RISK_PCT
from src.portfolio import ContractMeta, PositionManager, Signal
from src.strategies.contracts import Decision, SignalType
from src.trade_journal import make_position_id

UTC = timezone.utc


class BrokerExecutionAdapter:
    """Адаптер исполнения: преобразует `Decision` в `Signal` и вызывает брокера.

    Скрывает портфельные проверки (риск, размер, ГО) — наружу публикует протокол
    `ExecutionPort.execute()`. Размер риска (`risk_pct`) берётся из
    `Decision.risk_pct` или дефолта конфигурации `MAX_RISK_PCT`.
    """

    def __init__(self, broker: BrokerPort, manager: PositionManager):
        self.broker = broker
        self.manager = manager
        self._contracts: dict[str, ContractMeta] = {}

    def set_contracts(self, contracts: dict[str, ContractMeta]) -> None:
        self._contracts.update(contracts)
        if isinstance(self.broker, JournalBroker):
            self.broker.set_contracts(contracts)

    def execute(
        self,
        decision: Decision,
        instrument,
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
    ) -> object | None:
        ticker = getattr(instrument, "ticker", None)
        if not ticker or decision.signal_type is SignalType.HOLD:
            return None
        now = datetime.now(UTC).replace(microsecond=0)
        if decision.exit_reason is not None:
            return self._close_existing(ticker, decision.price, now, decision.exit_reason)

        contract = self._contracts.get(ticker)
        if contract is None:
            contract = ContractMeta(
                ticker=ticker, price_step=0, step_cost=0, go_buy=0, go_sell=0,
            )
            self._contracts[ticker] = contract

        side = "BUY" if decision.signal_type == SignalType.BUY else "SELL"
        risk_pct = float(getattr(decision, "risk_pct", None) or MAX_RISK_PCT)
        if not (0 < risk_pct <= 100):
            risk_pct = MAX_RISK_PCT
        stop_distance = None
        if decision.stop_loss is not None and decision.price:
            stop_distance = (decision.price - decision.stop_loss) / decision.price * 100
        signal = Signal(
            position_id=make_position_id(ticker),
            ticker=ticker,
            side=side,
            entry_price=decision.price,
            stop_price=decision.stop_loss,
            stop_distance_pct=stop_distance,
            risk_pct=risk_pct,
            take_profit=decision.take_profit,
            timeframe=timeframe or getattr(decision, "timeframe", "") or "",
            source=_source_label(decision, filter_profile),
        )
        return self._size_and_place(signal, contract, now, risk_pct)

    def _size_and_place(
        self,
        signal: Signal,
        contract: ContractMeta,
        now: datetime,
        risk_pct: float,
    ) -> object | None:
        out = self.manager.evaluate_signals([signal], self._contracts)[0]
        sized = Signal(
            position_id=signal.position_id,
            ticker=signal.ticker,
            side=signal.side,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            stop_distance_pct=signal.stop_distance_pct,
            risk_pct=out.risk_pct,
            take_profit=signal.take_profit,
            timeframe=signal.timeframe,
            source=signal.source,
            risk_rub=out.risk_rub,
            qty=out.qty,
        )
        return self.broker.place_order(sized, contract, now, reject_reason=out.reason or "qty-negative")

    def _close_existing(self, ticker: str, price: float, now: datetime, reason: str) -> object | None:
        for pos in self.manager.positions.values():
            if pos.ticker == ticker and pos.qty > 0:
                result = self.broker.close_position(pos, price, now, "signal")
                return result
        return None


def _source_label(decision: Decision, filter_profile: str) -> str:
    base = str(decision.strategy_name or "")
    if filter_profile:
        return f"{base} [{filter_profile}]"
    return base