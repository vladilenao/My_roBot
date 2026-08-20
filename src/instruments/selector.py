2
from datetime import datetime, timedelta, timezone
from t_tech.invest import InstrumentStatus

from src.api.client import client_context
from src.api.instruments import find_working_instrument
from src.api.retry import api_call_with_retry


RTS_STOCK_TICKERS = [
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN",
    "NVTK", "YDEX", "TCSG", "VTBR", "MTSS",
    "PHOR", "ALRS", "MOEX", "PLZL", "TATN",
]

FUTURES_BASES = [
    {"prefix": "RI", "name": "RTS",  "label": "Индекс РТС"},
    {"prefix": "BR", "name": "BR",   "label": "Нефть Brent"},
    {"prefix": "Si", "name": "Si",   "label": "Доллар – Рубль"},
    {"prefix": "SR", "name": "SBRF", "label": "Сбер Банк"},
    {"prefix": "ED", "name": "ED",   "label": "Евро – Доллар"},
    {"prefix": "NG", "name": "NG",   "label": "Природный газ"},
    {"prefix": "GD", "name": "GOLD", "label": "Золото"},
    {"prefix": "SV", "name": "SILV", "label": "Серебро"},
]

FUTURES_TTL = timedelta(days=183)


def fetch_active_futures(client):
    now = datetime.now(timezone.utc)
    cutoff = now + FUTURES_TTL
    resp = api_call_with_retry(
        client.instruments.futures, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
    )

    result = []
    for f in resp.instruments:
        name_lower = f.name.lower()
        if "микро" in name_lower or "мини" in name_lower:
            continue
        if f.expiration_date > cutoff:
            continue
        if f.expiration_date < now:
            continue

        ticker_upper = f.ticker.upper()
        for base in FUTURES_BASES:
            if ticker_upper.startswith(base["prefix"].upper()):
                contract_name = f.name.split()[0]
                result.append((contract_name, f.ticker, "future", f.expiration_date))
                break

    result.sort(key=lambda x: x[3])
    return [(display, ticker, inst_type) for display, ticker, inst_type, _ in result]


def _ask_choice(prompt, options):
    while True:
        raw = input(prompt).strip().lower()
        if raw in options:
            return raw
        allowed = "/".join(options)
        print(f"Неверный ввод. Допустимые варианты: {allowed}")


def _validate_instruments(client, tickers, inst_type):
    valid = []
    for ticker in tickers:
        try:
            find_working_instrument(client, ticker, inst_type)
            valid.append((ticker, inst_type))
            print(f"  ✓ {ticker} ({inst_type})")
        except ValueError as e:
            print(f"  ✗ {ticker}: {e}")
        except Exception as e:
            print(f"  ✗ {ticker}: ошибка API — {e}")
    return valid


def _select_from_list(client, entries, inst_type):
    if not entries:
        print("  Нет доступных инструментов.")
        return []
    print()
    for i, entry in enumerate(entries, 1):
        if isinstance(entry, tuple):
            display = entry[0]
        else:
            display = entry
        print(f"  {i}. {display}")
    print(f"\nВведите номера через запятую (например: 1,3,5):")
    print("Введите пустую строку для пропуска.\n")

    raw = input("Ваш выбор: ").strip()
    if not raw:
        return []

    selected = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(entries):
                if isinstance(entries[idx], tuple):
                    selected.append(entries[idx][1])
                else:
                    selected.append(entries[idx].upper())
            else:
                print(f"  ⚠ Номер {part} вне диапазона (1-{len(entries)})")
        else:
            selected.append(part.upper())

    if not selected:
        return []

    print()
    return _validate_instruments(client, selected, inst_type)


def _deduplicate(instruments):
    seen = set()
    result = []
    for ticker, inst_type in instruments:
        if ticker not in seen:
            seen.add(ticker)
            result.append((ticker, inst_type))
    return result


def _show_current(instruments):
    if instruments:
        print(f"\nТекущий список ({len(instruments)}):")
        for t, it in instruments:
            print(f"  - {t} ({it})")


def select_instruments():
    instruments = []
    print("=== Выбор торговых инструментов ===\n")

    with client_context() as client:
        while True:
            print("Выберите тип инструментов:")
            print("  1. Акции (индекс РТС)")
            print("  2. Фьючерсы")
            choice = _ask_choice("Ваш выбор (1/2): ", ("1", "2"))

            if choice == "1":
                found = _select_from_list(client, RTS_STOCK_TICKERS, "share")
                instruments.extend(found)
                instruments = _deduplicate(instruments)
            else:
                print("\nЗагрузка фьючерсов из API...")
                try:
                    futures = fetch_active_futures(client)
                except Exception as e:
                    print(f"  Ошибка загрузки фьючерсов: {e}")
                    futures = []
                if not futures:
                    print("  Нет фьючерсов с экспирацией в ближайшие 6 месяцев.")
                else:
                    found = _select_from_list(client, futures, "future")
                    instruments.extend(found)
                    instruments = _deduplicate(instruments)

            _show_current(instruments)

            print("\nДобавить ещё инструменты?")
            again = _ask_choice(" (да/нет): ", ("да", "нет", "д", "n", "y", "yes", "no"))
            if again in ("нет", "n", "no"):
                break
            print()

    if not instruments:
        print("\nНеобходимо выбрать хотя бы один инструмент.")
        return select_instruments()

    print(f"\nИтого выбрано: {len(instruments)}")
    for t, it in instruments:
        print(f"  - {t} ({it})")
    print()

    return instruments
