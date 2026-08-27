from datetime import datetime, timedelta, timezone
from t_tech.invest import InstrumentStatus

from src.api.client import client_context
from src.api.instruments import find_working_instrument
from src.api.retry import api_call_with_retry
from src.data.timeutil import to_naive


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


def _format_futures_display(contract_name, base_name, label):
    return f"{base_name} ({label}) — {contract_name}"


def fetch_active_futures(client):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now + FUTURES_TTL
    resp = api_call_with_retry(
        client.instruments.futures, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
    )

    by_base = {b["prefix"]: [] for b in FUTURES_BASES}
    for f in resp.instruments:
        name_lower = f.name.lower()
        if "микро" in name_lower or "мини" in name_lower:
            continue
        expiry = to_naive(f.expiration_date)
        if expiry > cutoff:
            continue
        if expiry < now:
            continue

        ticker_upper = f.ticker.upper()
        for base in FUTURES_BASES:
            if ticker_upper.startswith(base["prefix"].upper()):
                contract_name = f.name.split()[0]
                display = _format_futures_display(contract_name, base["name"], base["label"])
                by_base[base["prefix"]].append((display, f.ticker, "future", expiry))
                break

    result = []
    for base in FUTURES_BASES:
        entries = by_base[base["prefix"]]
        entries.sort(key=lambda x: x[3])
        result.extend(entries)

    return [(display, ticker, inst_type) for display, ticker, inst_type, _ in result]


def _ask_choice(prompt, options):
    while True:
        raw = input(prompt).strip().lower()
        if raw in options:
            return raw
        allowed = "/".join(options)
        print(f"Неверный ввод. Допустимые варианты: {allowed}")


def _validate_instruments(client, entries, inst_type):
    valid = []
    for entry in entries:
        if isinstance(entry, tuple):
            display_name, ticker, _ = entry
        else:
            display_name = entry
            ticker = entry
        try:
            find_working_instrument(client, ticker, inst_type)
            valid.append((display_name, ticker, inst_type))
            print(f"  ✓ {display_name} ({inst_type})")
        except ValueError as e:
            print(f"  ✗ {display_name}: {e}")
        except Exception as e:
            print(f"  ✗ {display_name}: ошибка API — {e}")
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

    selected_entries = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(entries):
                selected_entries.append(entries[idx])
            else:
                print(f"  ⚠ Номер {part} вне диапазона (1-{len(entries)})")
        else:
            ticker = part.upper()
            selected_entries.append((ticker, ticker, inst_type) if inst_type == "share" else (ticker, ticker, inst_type))

    if not selected_entries:
        return []

    print()
    return _validate_instruments(client, selected_entries, inst_type)


def _deduplicate(instruments):
    seen = set()
    result = []
    for item in instruments:
        display_name, ticker, inst_type = item
        if ticker not in seen:
            seen.add(ticker)
            result.append(item)
    return result


def _show_current(instruments):
    if instruments:
        print(f"\nТекущий список ({len(instruments)}):")
        for display_name, ticker, inst_type in instruments:
            print(f"  - {display_name} ({inst_type})")


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
                stock_entries = [(t, t, "share") for t in RTS_STOCK_TICKERS]
                found = _select_from_list(client, stock_entries, "share")
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
    for display_name, ticker, inst_type in instruments:
        print(f"  - {display_name} ({inst_type})")
    print()

    return instruments
