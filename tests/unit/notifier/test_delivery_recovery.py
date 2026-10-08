"""Recovery-хранилище Telegram не выполняет настоящий HTTP."""
from datetime import datetime, timedelta, timezone

from src.notifier.telegram_delivery import DeliveryRepository
from src.notifier.telegram_transport import ApiResult


def test_operation_preserves_png_and_payload_before_http(tmp_path):
    repo = DeliveryRepository(tmp_path / "delivery.db", "ns")
    operation_id = repo.save_operation("signal:post", "sendPhoto", {"caption": "Тест"}, b"\x89PNG-data", trade_id="trade")

    material = repo.operation_material(operation_id)

    assert material["data"] == {"caption": "Тест"}
    assert material["photo"] == b"\x89PNG-data"
    assert material["trade_id"] == "trade"
    repo.close()


def test_cleanup_removes_only_expired_blob_and_keeps_operation(tmp_path):
    repo = DeliveryRepository(tmp_path / "delivery.db", "ns")
    old = repo.save_operation("old", "sendPhoto", {"caption": "old"}, b"old")
    fresh = repo.save_operation("fresh", "sendPhoto", {"caption": "fresh"}, b"fresh")
    with repo.connection:
        repo.connection.execute("UPDATE telegram_files SET created_at=? WHERE file_id=(SELECT file_id FROM operations WHERE operation_id=?)", ((datetime.now(timezone.utc) - timedelta(days=7, seconds=1)).isoformat(), old))

    assert repo.cleanup_files(datetime.now(timezone.utc)) == 1
    assert repo.operation_material(old)["media_error"].startswith("Картинка удалена")
    assert repo.operation_material(fresh)["photo"] == b"fresh"
    assert repo.operation("old")["status"] == "pending"
    repo.close()


def test_exactly_seven_days_is_retained(tmp_path):
    now = datetime(2026, 1, 8, tzinfo=timezone.utc)
    repo = DeliveryRepository(tmp_path / "delivery.db", "ns")
    operation_id = repo.save_operation("boundary", "sendPhoto", {}, b"png")
    with repo.connection:
        repo.connection.execute("UPDATE telegram_files SET created_at=?", ((now - timedelta(days=7)).isoformat(),))

    assert repo.cleanup_files(now) == 0
    assert repo.operation_material(operation_id)["photo"] == b"png"
    repo.close()


def test_history_and_confirmed_receipt_are_persisted(tmp_path):
    repo = DeliveryRepository(tmp_path / "delivery.db", "ns")
    operation_id = repo.save_operation("post", "sendMessage", {"text": "x"})

    assert repo.begin_operation(operation_id)
    repo.finish_operation(operation_id, ApiResult(True, {"message_id": 9}))

    assert repo.operation("post")["status"] == "confirmed"
    history = repo.connection.execute("SELECT source,status FROM attempt_history").fetchone()
    assert tuple(history) == ("initial", "confirmed")
    assert not repo.begin_operation(operation_id, source="manual")
    repo.close()


def test_v1_receipt_is_visible_but_cannot_be_replayed(tmp_path):
    path = tmp_path / "delivery.db"
    repo = DeliveryRepository(path, "ns")
    assert repo.begin_attempt("old:post")
    repo.finish_attempt("old:post", ApiResult(False, uncertain=True))
    with repo.connection:
        repo.connection.execute("PRAGMA user_version=1")
        repo.connection.execute("DROP TABLE operations")
        repo.connection.execute("DROP TABLE telegram_files")
        repo.connection.execute("DROP TABLE attempt_history")
    repo.close()

    migrated = DeliveryRepository(path, "ns")
    row = migrated.pending()[0]
    material = migrated.operation_material(row["operation_id"])
    assert row["method"] == "legacy"
    assert material["media_error"].startswith("Картинка удалена")
    migrated.close()
