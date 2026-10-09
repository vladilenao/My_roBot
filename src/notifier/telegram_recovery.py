"""Автономные команды просмотра, повторной доставки и очистки Telegram."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from src.notifier.telegram_delivery import DeliveryRepository, namespace
from src.notifier.telegram_navigation import descriptor, register_confirmed, repair_navigation
from src.notifier.telegram_transport import TelegramTransport


def _period(day_from: str | None, day_to: str | None, offset_hours: float):
    if not day_from and not day_to:
        return None, None
    zone = timezone(timedelta(hours=offset_hours))
    start = datetime.combine(datetime.fromisoformat(day_from).date(), time.min, zone).astimezone(timezone.utc) if day_from else None
    end = datetime.combine(datetime.fromisoformat(day_to).date() + timedelta(days=1), time.min, zone).astimezone(timezone.utc) if day_to else None
    return start.isoformat() if start else None, end.isoformat() if end else None


def _repo(config, state_dir, *, readonly=False):
    path = state_dir / "telegram_delivery.sqlite3"
    return DeliveryRepository(path, namespace(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHANNEL_ID, path), readonly=readonly)


def list_pending(config, state_dir, *, trade_id=None, day_from=None, day_to=None) -> int:
    start, end = _period(day_from, day_to, config.BAR_TIME_TZ_OFFSET_HOURS)
    try:
        repo = _repo(config, state_dir, readonly=True)
    except Exception:
        print("Сохранённых уведомлений Telegram нет.")
        return 0
    try:
        rows = repo.pending(trade_id=trade_id, date_from=start, date_to=end)
        if not rows:
            print("Неподтверждённых уведомлений Telegram нет.")
            return 0
        for row in rows:
            media = "картинка удалена: срок хранения 7 дней" if row["media_expired"] else "материалы сохранены"
            print(f"{row['operation_id']} | {row['status']} | {row['method']} | {row['trade_id'] or 'без сделки'} | {row['created_at']} | {media}")
        return 0
    finally:
        repo.close()


def retry(config, state_dir, *, operation_id=None, trade_id=None, day_from=None, day_to=None, include_uncertain=False) -> int:
    if operation_id is None and not trade_id and not (day_from or day_to):
        print("Укажите --id, --trade-id или --from/--to для переотправки Telegram.")
        return 2
    start, end = _period(day_from, day_to, config.BAR_TIME_TZ_OFFSET_HOURS)
    repo = _repo(config, state_dir)
    transport = TelegramTransport(config.CLOUDFLARE_URL, config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHANNEL_ID, config.NOTIFIER_TELEGRAM_REQUEST_TIMEOUT)
    try:
        rows = repo.pending(trade_id=trade_id, date_from=start, date_to=end, include_confirmed=True)
        if operation_id is not None:
            rows = [row for row in rows if row["operation_id"] == operation_id]
        if not rows:
            print("Подходящих уведомлений нет.")
            return 0
        trades = sorted({row["trade_id"] for row in rows if row["trade_id"]})
        roots = {}
        selected = {row["operation_id"]: row for row in rows}
        for trade in trades:
            register_confirmed(repo, trade)
            candidates = [op for op in repo.trade_operations(trade) if descriptor(op, trade)["root"]]
            if candidates:
                root_id = candidates[0]["operation_id"]
                roots[trade] = root_id
                if repo.root(trade) is None and root_id not in selected:
                    root_row = next(r for r in repo.pending(trade_id=trade, include_confirmed=True) if r["operation_id"] == root_id)
                    selected[root_id] = root_row
                    print(f"Добавлена зависимость — основная карточка: {root_id}.")
        rows = sorted(selected.values(), key=lambda row: (row["operation_id"] not in roots.values(), row["operation_id"]))
        uncertain = [row for row in rows if row["status"] == "uncertain" and row["event_key"] != "navigation"]
        if uncertain and not include_uncertain:
            print("Неопределённые доставки пропущены: добавьте --include-uncertain, это может создать дубль.")
        elif uncertain:
            print("Внимание: повтор неопределённых доставок может создать дубликаты в Telegram.")
        send_ids = [str(row["operation_id"]) for row in rows if row["status"] not in {"confirmed", "superseded"}
                    and (row["status"] != "uncertain" or include_uncertain) and row["event_key"] != "navigation"]
        print("Выбраны для переотправки (после проверки зависимостей): " + (", ".join(send_ids) or "нет"))
        if trades:
            print("Будет восстановлена навигация уже доставленных сообщений сделок: " + ", ".join(trades))
        confirmed = failed = uncertain_count = blocked = skipped = 0
        for row in rows:
            if row["status"] in {"confirmed", "superseded"} or row["event_key"] == "navigation":
                skipped += 1
                continue
            if row["status"] == "uncertain" and not include_uncertain:
                blocked += 1
                continue
            trade = row["trade_id"]
            if trade in roots and row["operation_id"] != roots[trade] and repo.root(trade) is None:
                blocked += 1
                print(f"{row['operation_id']}: основная карточка {roots[trade]} не подтверждена; отправка заблокирована.")
                continue
            material = repo.operation_material(row["operation_id"])
            if material is None or material.get("media_error"):
                blocked += 1
                print(f"{row['operation_id']}: {material.get('media_error') if material else 'материалы не найдены'}")
                continue
            if not repo.begin_operation(row["operation_id"], source="manual"):
                continue
            response = transport.request(material["method"], material["data"], material.get("photo"))
            repo.finish_operation(row["operation_id"], response)
            if response.ok:
                confirmed += 1
                if trade:
                    register_confirmed(repo, trade)
            elif response.uncertain:
                uncertain_count += 1
            else:
                failed += 1
        navigation_failed = sum(not repair_navigation(repo, transport, trade) for trade in trades)
        print(f"Telegram: подтверждено {confirmed}, отказов {failed}, неопределённых {uncertain_count}, "
              f"заблокировано {blocked}, пропущено {skipped}; ошибок навигации {navigation_failed}.")
        return 0 if not (failed or uncertain_count or blocked or navigation_failed) else 1
    finally:
        repo.close()


def cleanup(config, state_dir) -> int:
    repo = _repo(config, state_dir)
    try:
        count = repo.cleanup_files()
        print(f"Telegram: удалено файлов старше 7 дней: {count}.")
        return 0
    finally:
        repo.close()
