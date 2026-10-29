from dataclasses import dataclass
from types import MappingProxyType

from t_tech.invest import Client, CandleInterval, SubscriptionInterval
from t_tech.invest.schemas import CandleSource

from src.config import TINKOFF_TOKEN
from src.logging_setup import get_logger
from src.api.deadline import RpcDeadline

log = get_logger(__name__)


def _broker_telemetry_off(client) -> None:
    """Замена ``init_error_hub`` SDK брокера: Sentry T-Bank не включается."""
    return None


async def _async_broker_telemetry_off(client) -> None:
    return None


def disable_broker_telemetry() -> bool:
    """Отключает телеметрию SDK T-Bank Invest (Sentry разработчика SDK).

    ``Client.__enter__`` SDK вызывает ``init_error_hub``, который включает
    ``sentry_sdk`` на весь процесс со встроенным адресом T-Bank: интеграция
    с ``logging`` собирает наши записи лога (breadcrumbs) мимо маскирования
    секретов в файловом логе, а сбои потоков ``UNAVAILABLE`` уходят наружу.
    Выключателя в SDK нет, поэтому точки вызова в ``t_tech.invest.clients``
    заменяются пустыми функциями. ``False`` — точки не найдены (SDK изменился).
    """
    from t_tech.invest import clients

    if not hasattr(clients, "init_error_hub") or not hasattr(clients, "async_init_error_hub"):
        log.warning("SDK брокера: точка включения телеметрии не найдена — проверьте отключение после обновления SDK")
        return False
    clients.init_error_hub = _broker_telemetry_off
    clients.async_init_error_hub = _async_broker_telemetry_off
    return True


disable_broker_telemetry()

# Совместимый профиль текущей истории: UNSPECIFIED, а не EXCHANGE-only.
# Имя существующей БД tbank_exchange не меняет фактический состав источника.
LIVE_CANDLE_SOURCE = CandleSource.CANDLE_SOURCE_UNSPECIFIED


@dataclass(frozen=True)
class NativeCandleInterval:
    """Именованный контракт одного нативного ТФ двух транспортов."""

    rpc: CandleInterval
    stream: SubscriptionInterval


# SDK helper поддерживает только 1m/5m. Числовое совпадение enum не является
# контрактом: каждый используемый приложением ТФ указан явно по имени.
NATIVE_CANDLE_INTERVALS = MappingProxyType({
    "1m": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_1_MIN,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE),
    "5m": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_5_MIN,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_FIVE_MINUTES),
    "15m": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_15_MIN,
                               SubscriptionInterval.SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES),
    "30m": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_30_MIN,
                               SubscriptionInterval.SUBSCRIPTION_INTERVAL_30_MIN),
    "1h": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_HOUR,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_HOUR),
    "4h": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_4_HOUR,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_4_HOUR),
    "1d": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_DAY,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_DAY),
    "1w": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_WEEK,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_WEEK),
    "1M": NativeCandleInterval(CandleInterval.CANDLE_INTERVAL_MONTH,
                              SubscriptionInterval.SUBSCRIPTION_INTERVAL_MONTH),
})


def native_candle_interval(timeframe: str) -> NativeCandleInterval:
    """Неподдержанный ТФ отклоняется, а не собирается скрыто из минут."""
    try:
        return NATIVE_CANDLE_INTERVALS[timeframe]
    except KeyError:
        raise ValueError(f"Нет нативного контракта свечей для ТФ {timeframe!r}") from None


class ClientProvider:
    """Порт источника данных: контекстный менеджер клиента выбранного режима.

    Остальной контур (загрузчик свечей, поиск инструмента, выбор инструментов)
    работает с любым провайдером одинаково: меняется только реализация.
    """

    def client_context(self, token=None):
        raise NotImplementedError

    def calendar_source(self, store, *, token=None, request_budget=None, horizon_days=7, refresh_seconds=3600):
        raise NotImplementedError


class TinkoffClientProvider(ClientProvider):
    """Боевой режим: аутентифицированный клиент T-Банка Invest API."""

    def client_context(self, token=None):
        return Client(token or TINKOFF_TOKEN, interceptors=[RpcDeadline()])

    def stream_session(self, token=None, *, control_queue_max_messages=128):
        """Необязательный live-порт; исторический provider остаётся unary."""
        from src.api.market_data_stream import MarketDataStreamSession
        return MarketDataStreamSession(
            self.client_context(token), control_queue_max_messages=control_queue_max_messages,
        )

    def calendar_source(self, store, *, token=None, request_budget=None, horizon_days=7, refresh_seconds=3600):
        from src.market_calendar.service import BrokerScheduleSource

        return BrokerScheduleSource(
            store, self, token=token, request_budget=request_budget,
            horizon_days=horizon_days, refresh_seconds=refresh_seconds,
        )


_DEFAULT_PROVIDER = TinkoffClientProvider()


def default_client_provider() -> ClientProvider:
    """Провайдер боевого режима по умолчанию."""
    return _DEFAULT_PROVIDER


def get_client():
    """Возвращает клиента Tinkoff Invest API."""
    return Client(TINKOFF_TOKEN)


def client_context(token=None):
    """Возвращает контекстный менеджер для клиента API."""
    return _DEFAULT_PROVIDER.client_context(token)
