from abc import ABC, abstractmethod
from datetime import datetime
from typing import Mapping, Optional

from src.portfolio import BrokerEvent, ContractMeta, OrderResult, Signal


class BrokerPort(ABC):
    """Порт брокерского исполнения.

    Исполнитель получает решения (сигналы на открытие) и котировки, возвращает
    результаты + события для уведомлений. Реализация — симуляция через
    дневник сделок (`JournalBroker`); интерфейс инвариантен для бэкенда.
    """

    @abstractmethod
    def load_state(self) -> None:
        """Восстановление состояния (позиции, заявки, счёт) из персистентного хранилища."""

    @abstractmethod
    def place_order(self, signal: Signal, contract: ContractMeta, now: datetime) -> OrderResult:
        """Размещение заявки на открытие позиции (entry/scale)."""

    @abstractmethod
    def track_bar(
        self,
        now: datetime,
        prices: Mapping[str, tuple[float, float, float]],
        contracts: Mapping[str, ContractMeta],
    ) -> list[OrderResult]:
        """Обработка закрытой свечи: защитные стопы, TTL, исполнение заявок, over_risk."""

    @abstractmethod
    def cancel_order(self, order_id: int, reason: str) -> OrderResult:
        """Отмена размещённой, но не исполненной заявки."""

    @abstractmethod
    def run_clearing(self, now: datetime) -> list[OrderResult]:
        """Клиринг FORTS: снимок, отмена отложенных, переустановка защитных стопов."""

    @abstractmethod
    def run_clearing_if_due(self, now: datetime) -> bool:
        """Выполнить клиринг, если момент его наступил (сравнение с графиком)."""

    @abstractmethod
    def drain_events(self) -> list[BrokerEvent]:
        """Забрать накопленные события для уведомлений."""

    @abstractmethod
    def contract_for(self, ticker: str) -> Optional[ContractMeta]:
        """Доступ к метаданным контракта (шаг цены и т.п.) с внутренним кэшем."""