"""Unit-тесты модуля logging_setup.py."""
import logging
import logging.handlers
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.logging_setup import (
    ContextLoggerAdapter,
    correlation_id_var,
    get_logger,
    service_uid_var,
    setup_logging,
)


class _CaptureHandler(logging.Handler):
    """Хэндлер, собирающий записи в список (для проверок без caplog)."""

    def __init__(self, records: list) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


class TestSetupLogging:
    """Проверка setup_logging()."""

    def test_creates_rotating_file_handler(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        root = logging.getLogger()
        handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(handlers) == 1, "Должен быть один RotatingFileHandler"

    def test_removes_console_handlers(self, tmp_path: Path) -> None:
        root = logging.getLogger()
        console = logging.StreamHandler()
        root.addHandler(console)
        assert console in root.handlers

        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        assert console not in root.handlers, "Консольный хэндлер должен быть удалён"

    def test_sets_service_uid(self, tmp_path: Path) -> None:
        uid = "b7e3a1c4-92f8-4d5e-a016-7f8b2c3d4e5a"
        log_file = tmp_path / "test.log"
        setup_logging(service_uid=uid, log_file=str(log_file))
        assert service_uid_var.get() == uid

    def test_sets_correct_level(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file), level="WARNING")
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_rotation_params(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(
            service_uid="test-uid",
            log_file=str(log_file),
            max_bytes=512,
            backup_count=3,
        )
        root = logging.getLogger()
        handler = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)][0]
        assert handler.maxBytes == 512
        assert handler.backupCount == 3

    def test_writes_log_entry(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        logger = get_logger("test_write")
        logger.info("Тестовая запись")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "Тестовая запись" in content


class TestGetLogger:
    """Проверка get_logger()."""

    def test_returns_context_logger_adapter(self) -> None:
        logger = get_logger("test.module")
        assert isinstance(logger, ContextLoggerAdapter)

    def test_uses_module_name(self) -> None:
        logger = get_logger("my.module")
        assert logger.logger.name == "my.module"

    def test_adapter_injects_service_uid(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="svc-123", log_file=str(log_file))
        logger = get_logger("test_inject")
        logger.info("проверка service_uid")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "[svc-123]" in content

    def test_adapter_injects_correlation_id(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        correlation_id_var.set("abcd1234")
        try:
            logger = get_logger("test_corr")
            logger.info("проверка correlation_id")
            for h in logging.getLogger().handlers:
                h.flush()
            content = log_file.read_text(encoding="utf-8")
            assert "[tick:abcd1234]" in content
        finally:
            correlation_id_var.set(None)

    def test_empty_correlation_id_when_none(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        correlation_id_var.set(None)
        logger = get_logger("test_empty_corr")
        logger.info("без correlation")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "[tick:]" in content


class TestCorrelationIdVar:
    """Проверка contextvars."""

    def test_default_is_none(self) -> None:
        assert correlation_id_var.get() is None

    def test_set_and_reset(self) -> None:
        correlation_id_var.set("12345678")
        assert correlation_id_var.get() == "12345678"
        correlation_id_var.set(None)
        assert correlation_id_var.get() is None

    def test_default_service_uid_is_empty(self) -> None:
        # Вне setup_logging контекста значение по умолчанию
        assert isinstance(service_uid_var.get(), str)


class TestLogMatrix:
    """Проверка что уровень логирования соответствует design D1."""

    def test_debug_level_logs_write_to_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file), level="DEBUG")
        logger = get_logger("test_debug")
        logger.debug("отладочное сообщение")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "отладочное сообщение" in content

    def test_info_level_logs_write_to_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file), level="INFO")
        logger = get_logger("test_info")
        logger.info("информационное сообщение")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "информационное сообщение" in content

    def test_warning_level_logs_write_to_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file), level="WARNING")
        logger = get_logger("test_warning")
        logger.warning("предупреждение")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "предупреждение" in content

    def test_error_level_logs_write_to_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file), level="ERROR")
        logger = get_logger("test_error")
        logger.error("ошибка")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "ошибка" in content


class TestNotifierLoggingIsolation:
    """Проверка что UI-уведомления НЕ проходят через логгер (design D1)."""

    def test_console_channel_does_not_emit_log_records(self, caplog) -> None:
        from src.events.event import Event
        from src.notifier import ConsoleChannel

        with caplog.at_level(logging.DEBUG):
            ConsoleChannel().handle(Event.error(operation="проверка"))

        assert caplog.records == [], "канал не должен писать в логгер"

    def test_console_channel_prints_to_stdout(self, capsys) -> None:
        from src.events.event import Event
        from src.notifier import ConsoleChannel

        ConsoleChannel().handle(Event.heartbeat(tick_count=7, error_count=1))

        out = capsys.readouterr().out
        assert "СИСТЕМА       7 тактов работы · 1 ошибка" in out
        assert out.endswith("\n")

    def test_logger_output_does_not_go_to_stdout(self, tmp_path: Path, capsys) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        get_logger("test_stdout").warning("служебное сообщение")

        assert capsys.readouterr().out == "", "Логгер не должен писать в stdout"

    def test_logger_output_does_not_go_to_stderr(self, tmp_path: Path, capsys) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))
        get_logger("test_stderr").error("служебная ошибка")

        captured = capsys.readouterr()
        assert captured.err == "", "Логгер не должен писать в stderr"


class TestRetryLogging:
    """Проверка что замена print() на log.warning() в api/retry.py работает."""

    @patch("src.api.retry.time.sleep")
    def test_rate_limit_warns_to_log(self, mock_sleep, tmp_path: Path) -> None:
        from src.api.retry import api_call_with_retry

        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))

        records = []
        capture = _CaptureHandler(records)
        logging.getLogger("src.api.retry").addHandler(capture)
        try:
            fn = MagicMock(side_effect=[
                Exception("RESOURCE_EXHAUSTED"),
                "ok",
            ])
            result = api_call_with_retry(fn, max_retries=3, base_delay=1)
        finally:
            logging.getLogger("src.api.retry").removeHandler(capture)

        assert result == "ok"
        assert fn.call_count == 2
        warning = [r for r in records if r.levelno == logging.WARNING]
        assert warning, "Rate-limit должен попадать в лог как warning"
        assert "Rate limit" in warning[0].getMessage()

    @patch("src.api.retry.time.sleep")
    def test_rate_limit_warning_written_to_file(self, mock_sleep, tmp_path: Path) -> None:
        from src.api.retry import api_call_with_retry

        log_file = tmp_path / "test.log"
        setup_logging(service_uid="test-uid", log_file=str(log_file))

        fn = MagicMock(side_effect=[
            Exception("RESOURCE_EXHAUSTED"),
            "ok",
        ])
        result = api_call_with_retry(fn, max_retries=3, base_delay=1)
        assert result == "ok"

        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "Rate limit" in content

    def test_no_print_in_retry_module(self) -> None:
        """Проверка что в src/api/retry.py не осталось print()."""
        from src.api import retry as retry_module

        loader = retry_module.__loader__
        if loader is None or not hasattr(loader, "get_source"):
            pytest.skip("Нет доступа к исходнику retry.py")
        source = loader.get_source("src.api.retry")
        assert "print(" not in source, "В retry.py не должно остаться print()"

    def test_third_party_record_gets_context_fields(self, tmp_path: Path) -> None:
        """Запись логгера без адаптера (t_tech и др.) не должна падать в форматтере.

        Регрессия: форматтер требует %(service_uid)s, но сторонние логгеры
        эмитят чистые LogRecord без этих полей — хэндлер-фильтр обязан их
        встроить, иначе RotatingFileHandler.emit падает с ValueError.
        """
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="svc-456", log_file=str(log_file))

        foreign = logging.getLogger("t_tech.invest.logging")  # без ContextLoggerAdapter
        foreign.setLevel(logging.INFO)
        foreign.info("%s %s", "588f", "GetCandles")

        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "[svc-456]" in content
        assert "GetCandles" in content


# Поддельные значения: настоящие секреты в тестах не используются.
_FAKE_BOT_TOKEN = "123456789:" + "AAFakeTokenForTests_" + "x" * 15
_FAKE_BROKER_TOKEN = "t.fake-broker-token-for-tests-0123456789"
_FAKE_PROXY = "https://fake-proxy.example.workers.dev"


@pytest.fixture
def clean_secrets():
    from src import logging_setup

    saved = set(logging_setup._secrets)
    logging_setup._secrets.clear()
    yield
    logging_setup._secrets.clear()
    logging_setup._secrets.update(saved)


def _read_log(log_file: Path) -> str:
    for h in logging.getLogger().handlers:
        h.flush()
    return log_file.read_text(encoding="utf-8")


@pytest.mark.usefixtures("clean_secrets")
class TestSecretRedaction:
    """Секреты не попадают в файл лога ни из своих, ни из сторонних логгеров."""

    def test_urllib3_request_line_hides_token_and_proxy(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="svc", log_file=str(log_file),
                      secrets=(_FAKE_BOT_TOKEN, _FAKE_PROXY))
        foreign = logging.getLogger("urllib3.connectionpool")  # без адаптера, как в бою
        foreign.setLevel(logging.DEBUG)
        foreign.debug("Starting new HTTPS connection (%d): %s:%s", 1, "fake-proxy.example.workers.dev", 443)
        foreign.debug('%s://%s:%s "%s %s %s" %s %s', "https", "fake-proxy.example.workers.dev", 443,
                      "POST", f"/bot{_FAKE_BOT_TOKEN}/sendMessage", "HTTP/1.1", 200, None)

        content = _read_log(log_file)
        assert _FAKE_BOT_TOKEN not in content
        assert "fake-proxy.example.workers.dev" not in content
        assert "/bot[скрыто]/sendMessage" in content

    def test_unregistered_telegram_token_is_hidden_by_format(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="svc", log_file=str(log_file))
        get_logger("test_secret").warning("POST /bot%s/getChat", _FAKE_BOT_TOKEN)

        content = _read_log(log_file)
        assert _FAKE_BOT_TOKEN not in content
        assert "/bot[скрыто]/getChat" in content

    def test_registered_value_hidden_in_message_and_traceback(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        setup_logging(service_uid="svc", log_file=str(log_file), secrets=(_FAKE_BROKER_TOKEN, None, ""))
        logger = get_logger("test_secret")
        logger.info("token=%s", _FAKE_BROKER_TOKEN)
        try:
            raise RuntimeError(f"Authorization: Bearer {_FAKE_BROKER_TOKEN}")
        except RuntimeError:
            logger.exception("Сбой запроса")

        content = _read_log(log_file)
        assert _FAKE_BROKER_TOKEN not in content
        assert "token=[скрыто]" in content
        assert "Bearer [скрыто]" in content

    def test_short_values_and_ordinary_text_untouched(self, tmp_path: Path) -> None:
        from src.logging_setup import redact, register_secrets

        register_secrets(("abc",))
        text = "Свеча 2026-10-15T10:00:00 abc, цена 1.1238, заявка 12345:ok"
        assert redact(text) == text
