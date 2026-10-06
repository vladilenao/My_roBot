"""Клиент локального Historical Broker API Emulator.

Адаптер отдаёт ровно ту поверхность атрибутов, которую читает контур робота
(загрузчик свечей, поиск инструмента, выбор инструментов), поэтому в контуре
нет ветвлений по типу источника данных. Сетевой код остаётся здесь: адрес,
таймауты и разбор ошибок эмулятора; повтор запросов — в ``src/api/retry.py``.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from urllib import error as urlerror
from urllib import parse, request
from urllib.error import HTTPError

from t_tech.invest import schemas
from t_tech.invest.schemas import CandleInterval, InstrumentStatus, Quotation

from src.api.client import ClientProvider
from src.config import HISTORICAL_API_URL
from src.data.timeutil import to_aware_utc, to_naive
from src.logging_setup import get_logger

log = get_logger(__name__)

_INTERVALS = {
    CandleInterval.CANDLE_INTERVAL_1_MIN: "1m",
    CandleInterval.CANDLE_INTERVAL_5_MIN: "5m",
    CandleInterval.CANDLE_INTERVAL_15_MIN: "15m",
    CandleInterval.CANDLE_INTERVAL_30_MIN: "30m",
    CandleInterval.CANDLE_INTERVAL_HOUR: "1h",
    CandleInterval.CANDLE_INTERVAL_4_HOUR: "4h",
    CandleInterval.CANDLE_INTERVAL_DAY: "1d",
    CandleInterval.CANDLE_INTERVAL_WEEK: "1w",
    CandleInterval.CANDLE_INTERVAL_MONTH: "1M",
}

# Потолок свечей в одной странице: максимум эмулятора — меньше страниц на 1m.
_CANDLES_PAGE_LIMIT = 200_000

class EmulatorError(RuntimeError):
    """Ошибка локального эмулятора исторических данных."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.message = message
        self.status = status


def _quotation(value) -> Quotation:
    """Разбирает цену эмулятора в `units`/`nano` — как у SDK."""
    if value is None:
        return Quotation(units=0, nano=0)
    amount = Decimal(str(value))
    units = int(amount)
    nano = int(round((amount - units) * 1_000_000_000))
    if nano >= 1_000_000_000:
        units += 1
        nano -= 1_000_000_000
    return Quotation(units=units, nano=nano)


def _candle_time(value) -> datetime:
    """Время бара эмулятора (naive UTC) как tz-aware UTC, как отдаёт SDK."""
    if isinstance(value, str):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        moment = datetime.fromisoformat(str(value))
    return to_aware_utc(moment).to_pydatetime()


def _bounds(moment) -> str:
    return to_naive(moment).strftime("%Y-%m-%d %H:%M:%S")


def _expiration(value) -> datetime | None:
    """Дата экспирации карточки (ISO 8601 со смещением) как aware-время.

    Пустое или отсутствующее значение — это «экспирации нет», а не текущая
    дата: подстановка даты заблокировала бы вход по любому контракту без
    экспирации как по просроченному. Нераспознанный формат тоже трактуется
    как отсутствие экспирации и попадает в лог.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(str(value))
    except ValueError:
        log.warning("Эмулятор: не разобрана дата экспирации %r, считаем её отсутствующей", value)
        return None


class HistoricalInstruments:
    """Каталог инструментов эмулятора с той же поверхностью, что у SDK."""

    def __init__(self, client: HistoricalClient) -> None:
        self._client = client

    def shares(self, *, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_UNSPECIFIED, **_kw):
        return schemas.SharesResponse(
            instruments=self._client._catalog("share", schemas.Share)
        )

    def futures(self, *, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_UNSPECIFIED, **_kw):
        return schemas.FuturesResponse(
            instruments=self._client._catalog("future", schemas.Future)
        )

    def etfs(self, *, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_UNSPECIFIED, **_kw):
        return schemas.EtfsResponse(
            instruments=self._client._catalog("etf", schemas.Etf)
        )

    def currencies(self, *, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_UNSPECIFIED, **_kw):
        return schemas.CurrenciesResponse(
            instruments=self._client._catalog("currency", schemas.Currency)
        )

    def get_futures_margin(self, *, figi: str = "", instrument_id: str = ""):
        """ГО контракта из карточки инструмента.

        Поля маржи и стоимости шага эмулятор отдаёт в карточке; при их
        отсутствии деградация предсказуема: стоимость шага берётся из шага
        цены, ГО — ноль, что система трактует как «обеспечения нет».
        """
        found = self._client._find_instrument(figi=figi, instrument_id=instrument_id)
        step = _quotation(found.get("min_price_increment"))
        step_amount = found.get("min_price_increment_amount")
        return schemas.GetFuturesMarginResponse(
            min_price_increment=step,
            min_price_increment_amount=_quotation(
                step_amount if step_amount is not None else found.get("min_price_increment")
            ),
            initial_margin_on_buy=_quotation(found.get("initial_margin_on_buy")),
            initial_margin_on_sell=_quotation(found.get("initial_margin_on_sell")),
        )


class HistoricalClient:
    """Источник исторических данных вместо Tinkoff Invest API.

    Отвечает теми же объектами `t_tech.invest.schemas`, поэтому загрузчик
    свечей и поиск инструмента работают без изменений.
    """

    def __init__(self, base_url: str | None = None, *, timeout: float = 60.0) -> None:
        self._base_url = (base_url or HISTORICAL_API_URL).rstrip("/")
        self._timeout = timeout
        self.instruments = HistoricalInstruments(self)
        self._closed = False

    @property
    def base_url(self) -> str:
        return self._base_url

    def __enter__(self) -> HistoricalClient:
        self._closed = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._closed = True

    def ping(self) -> dict:
        """Проверка живости источника: ``GET /health``."""
        return self._get("/health")

    def get_all_candles(
        self,
        *,
        from_,
        to=None,
        interval=CandleInterval.CANDLE_INTERVAL_UNSPECIFIED,
        figi: str = "",
        instrument_id: str = "",
        candle_source_type=None,
    ):
        """Свечи эмулятора как генератор `HistoricCandle`.

        Эмулятор отдаёт данные постранично, поэтому страницы обходятся по
        `next_cursor` до пустого курсора. `limit` задан явно — максимумом
        эмулятора, чтобы на 1m не делать тысячи запросов. Граничная свеча не
        дублируется: курсор передаётся как исключительная нижняя граница.
        """
        params = {
            "instrument_id": instrument_id or self._uid_for(figi),
            "interval": self._interval(interval),
            "limit": _CANDLES_PAGE_LIMIT,
        }
        if from_ is not None:
            params["from"] = _bounds(from_)
        if to is not None:
            params["to"] = _bounds(to)
        cursor = None
        read = 0
        expected = None
        while True:
            page = dict(params)
            if cursor is not None:
                page["cursor"] = cursor
            payload = self._get("/api/v1/candles", page)
            rows = payload.get("candles") or []
            for row in rows:
                yield self._candle(row)
            read += len(rows)
            if expected is None:
                expected = payload.get("total")
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise EmulatorError(
                    "CURSOR_LOOP",
                    f"Эмулятор вернул повторяющийся курсор {next_cursor!r} для "
                    f"инструмента {params['instrument_id']} ({params['interval']})",
                )
            cursor = next_cursor
        if expected:
            if read < expected:
                log.warning(
                    "Эмулятор: прочитано %d свечей из %d заявленных "
                    "(%s %s, с %s по %s) — диапазон обрезан",
                    read, expected, params["instrument_id"], params["interval"],
                    params.get("from"), params.get("to"),
                )
            else:
                log.debug(
                    "Эмулятор: прочитано %d свечей из %d (%s %s)",
                    read, expected, params["instrument_id"], params["interval"],
                )

    # ── internals ──
    def _interval(self, interval) -> str:
        name = _INTERVALS.get(interval)
        if name is None:
            raise ValueError(f"Эмулятор не поддерживает интервал {interval!r}")
        return name

    def _candle(self, row: dict) -> schemas.HistoricCandle:
        return schemas.HistoricCandle(
            time=_candle_time(row["datetime"]),
            open=_quotation(row.get("open")),
            high=_quotation(row.get("high")),
            low=_quotation(row.get("low")),
            close=_quotation(row.get("close")),
            volume=int(row.get("volume") or 0),
        )

    def _catalog(self, instrument_type: str, schema) -> tuple:
        return tuple(
            self._instrument(row, schema)
            for row in self._instruments(instrument_type)
        )

    def _instruments(self, instrument_type: str) -> list[dict]:
        return self._get("/api/v1/instruments", {"instrument_type": instrument_type})

    def _find_instrument(self, *, figi: str = "", instrument_id: str = "") -> dict:
        if instrument_id:
            return self._get(f"/api/v1/instruments/{parse.quote(instrument_id)}")
        for row in self._get("/api/v1/instruments", {}):
            if row.get("figi") == figi:
                return row
        raise EmulatorError("INSTRUMENT_NOT_FOUND", f"Инструмент figi={figi} не найден", 404)

    def _uid_for(self, figi: str) -> str:
        if not figi:
            raise ValueError("Нужен instrument_id или figi")
        return self._find_instrument(figi=figi)["instrument_id"]

    def _instrument(self, row: dict, schema):
        fields = {
            "figi": row.get("figi", ""),
            "ticker": row.get("ticker", ""),
            "class_code": row.get("class_code", ""),
            "name": row.get("name", ""),
            "currency": row.get("currency", ""),
            "lot": int(row.get("lot") or 0),
            "uid": row.get("instrument_id", ""),
            "min_price_increment": _quotation(row.get("min_price_increment")),
        }
        if schema is schemas.Future:
            # Поверхность SDK требует заполнения целиком, поэтому поля маржи и
            # экспирации задаются всегда — но значениями карточки, а не
            # константами: пустое значение означает «источник не отдал», а не
            # «нулевое обеспечение».
            fields["expiration_date"] = _expiration(row.get("expiration_date"))
            fields["min_price_increment_amount"] = _quotation(
                row.get("min_price_increment_amount", row.get("min_price_increment"))
            )
            fields["initial_margin_on_buy"] = _quotation(row.get("initial_margin_on_buy"))
            fields["initial_margin_on_sell"] = _quotation(row.get("initial_margin_on_sell"))
        return schema(**fields)

    def _get(self, path: str, params: dict | None = None) -> object:
        url = f"{self._base_url}{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{parse.urlencode(query)}"
        log.debug("Эмулятор: GET %s", url)
        try:
            with request.urlopen(url, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise self._error(exc) from exc
        except urlerror.URLError as exc:
            raise EmulatorError("SOURCE_UNAVAILABLE", f"Эмулятор недоступен: {exc.reason}") from exc
        except TimeoutError as exc:
            raise EmulatorError(
                "SOURCE_TIMEOUT", f"Эмулятор не ответил за {self._timeout:g}s"
            ) from exc
        return json.loads(body) if body else {}

    def _error(self, exc: HTTPError) -> EmulatorError:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {})
        except Exception:  # noqa: BLE001 — тело ошибки может быть любым
            detail = {}
        return EmulatorError(
            detail.get("code", "INTERNAL_ERROR"),
            detail.get("message", exc.reason or "запрос отклонён"),
            exc.code,
        )


class EmulatorClientProvider(ClientProvider):
    """Провайдер исторического режима: локальный эмулятор без токена T-API."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 60.0) -> None:
        self._base_url = base_url or HISTORICAL_API_URL
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def client_context(self, token=None) -> HistoricalClient:
        return HistoricalClient(self._base_url, timeout=self._timeout)
