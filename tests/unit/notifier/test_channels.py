"""Unit-тесты каналов: консоль, порт канала, Telegram-очередь."""

import io
import time
from datetime import datetime, timezone

import pytest
import requests

from src.events import EventBus
from src.events.event import Event
from src.events.types import TRADING_EVENT_TYPES, EventType
from src.notifier.channel import Channel
from src.notifier.console import ConsoleChannel
from src.notifier.telegram import MESSAGE_LIMIT, TelegramChannel, _split_message


class _Stub(Channel):
    name = "stub"
    supported_types = frozenset({EventType.SIGNAL})

    def handle(self, event: Event) -> None: ...


class TestChannelPort:
    def test_port_cannot_be_instantiated(self) -> None:
        try:
            Channel()
        except TypeError:
            pass
        else:
            raise AssertionError("абстрактный порт нельзя инстанцировать")

    def test_subclass_must_declare_supported_types(self) -> None:
        class Empty(Channel):
            name = "empty"

            def handle(self, event: Event) -> None: ...

        try:
            Empty()
        except ValueError as exc:
            assert "supported_types" in str(exc)
        else:
            raise AssertionError("канал без supported_types должен отвергаться")

    def test_accepts_uses_supported_types(self) -> None:
        stub = _Stub()

        assert stub.accepts(Event.signal("NG-10.26", side="BUY", quantity=1, entry=1, stop=2))
        assert not stub.accepts(Event.heartbeat(tick_count=1, error_count=0))


class TestConsoleChannel:
    def test_prints_rendered_text(self, capsys) -> None:
        ConsoleChannel().handle(Event.heartbeat(tick_count=1, error_count=0))

        assert capsys.readouterr().out == "💓 Сердцебиение: тиков работы — 1, ошибок за период — 0.\n"

    def test_writes_to_given_stream(self) -> None:
        import io

        stream = io.StringIO()
        ConsoleChannel(stream=stream).handle(Event.error(operation="тик"))

        assert "bot_debug.log" in stream.getvalue()

    def test_skips_event_without_template(self, capsys) -> None:
        ConsoleChannel().handle(Event.rate_limited(source="tinkoff"))

        assert capsys.readouterr().out == ""

    def test_broken_pipe_disables_channel_without_error(self) -> None:
        class BrokenStream(io.StringIO):
            writes = 0

            def write(self, _s: str) -> int:
                BrokenStream.writes += 1
                raise BrokenPipeError(32, "Broken pipe")

        channel = ConsoleChannel(stream=BrokenStream())
        channel.handle(Event.heartbeat(tick_count=1, error_count=0))
        channel.handle(Event.heartbeat(tick_count=2, error_count=0))

        # Первая попытка упала в OSError (канал её пережил), вторая не делалась.
        assert BrokenStream.writes == 1

    def test_quiet_tick_prints_one_summary_through_bus(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(
            stream=stream,
            supported_types={EventType.DECISION, EventType.ERROR},
            tz_offset_hours=-7,
            now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc),
        )
        bus = EventBus()
        bus.subscribe(channel)

        bus.publish(Event.tick_started(tick_id="one"))
        for instrument in ("NG-9.26", "GAZP", "Si", "RTS"):
            for strategy in ("first", "second"):
                bus.publish(Event.decision(
                    instrument, outcome="no_signal", side="HOLD", strategy=strategy,
                ))
        bus.publish(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "● 10:15 ➜ ⏳ Нет сигналов (8 пар: NG-9.26, GAZP, Si, RTS)\n"

    def test_printed_event_suppresses_quiet_tick_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.decision("SBER", outcome="signal_buy", side="BUY", price=100))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "● SBER ➜ 🟢 ПОКУПКА (BUY) — Цена: 100.000\n"

    def test_filtered_hold_is_printed_and_suppresses_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.decision("SBER", outcome="filtered", side="HOLD", filtered_out=True))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "● SBER ➜ ❌ Отклонено фильтром.\n"

    def test_holds_for_different_timeframes_and_profiles_count_separately(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        for timeframe, profile in (("5m", "raw"), ("15m", "raw"), ("15m", "basic_levels")):
            channel.handle(Event.decision(
                "GAZP", outcome="no_signal", side="HOLD", timeframe=timeframe, filter_profile=profile,
            ))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "● 10:15 ➜ ⏳ Нет сигналов (3 пары: GAZP)\n"

    def test_disabled_decisions_do_not_produce_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, supported_types={EventType.ERROR})
        bus = EventBus()
        bus.subscribe(channel)

        bus.publish(Event.tick_started(tick_id="one"))
        bus.publish(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        bus.publish(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == ""

    def test_unfinished_tick_and_mismatched_finish_do_not_print_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="other", completed=True))
        channel.handle(Event.tick_finished(tick_id="one", completed=False))
        channel.handle(Event.tick_started(tick_id="two"))
        channel.handle(Event.tick_finished(tick_id="two", completed=True))

        assert stream.getvalue() == ""

    def test_summary_plural_and_midnight_moscow_time(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(
            stream=stream,
            now=lambda: datetime(2026, 10, 8, 22, 15, tzinfo=timezone.utc),
        )

        channel.handle(Event.tick_started(tick_id="one"))
        for _ in range(21):
            channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "● 01:15 ➜ ⏳ Нет сигналов (21 пара: GAZP)\n"

    @pytest.mark.parametrize("event", [Event.error(operation="тик"), Event.heartbeat(tick_count=1, error_count=0)])
    def test_printed_system_event_suppresses_summary(self, event) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(event)
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert "Нет сигналов" not in stream.getvalue()

    def test_events_between_ticks_do_not_suppress_next_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("SBER", outcome="signal_buy", side="BUY", price=100))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))
        channel.handle(Event.error(operation="между тиками"))
        channel.handle(Event.tick_started(tick_id="two"))
        channel.handle(Event.decision("", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="two", completed=True))

        assert stream.getvalue().endswith("● 10:15 ➜ ⏳ Нет сигналов (1 пара: контракт не указан)\n")

    def test_event_without_text_does_not_suppress_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.rate_limited(source="tinkoff"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "● 10:15 ➜ ⏳ Нет сигналов (1 пара: GAZP)\n"

    @pytest.mark.parametrize(
        "event",
        [
            Event.rejected("SBER", reason="нет объёма"),
            Event.broker_event(EventType.TRADE_OPENED, trade_id="trade-1", side="BUY", quantity=1, price=100),
        ],
    )
    def test_trade_and_rejection_events_suppress_summary(self, event) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(event)
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert "Нет сигналов" not in stream.getvalue()

    def test_close_discards_unfinished_holds(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.close()
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == ""


class TestSplitMessage:
    def test_short_message_is_untouched(self) -> None:
        assert _split_message("привет") == ["привет"]

    def test_long_message_is_split_on_line_boundaries(self) -> None:
        line = "x" * 1000
        text = "\n".join([line] * 5)

        chunks = _split_message(text, limit=2100)

        assert len(chunks) == 3
        assert all(len(chunk) <= 2100 for chunk in chunks)
        assert "".join(chunks).replace("\n", "") == line * 5

    def test_single_line_longer_than_limit_is_hard_split(self) -> None:
        chunks = _split_message("y" * 2500, limit=1000)

        assert [len(chunk) for chunk in chunks] == [1000, 1000, 500]

    def test_default_limit_is_telegram_limit(self) -> None:
        assert MESSAGE_LIMIT == 4096


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", body: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._body = body or {}

    def json(self) -> dict:
        if not self._body:
            raise ValueError("тело ответа не JSON")
        return self._body


class TestTelegramChannel:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        self.posts: list[dict] = []

        def fake_post(url, data, timeout):
            self.posts.append({"url": url, "text": data["text"]})
            return _FakeResponse(body={"ok": True, "result": {"message_id": len(self.posts)}})

        monkeypatch.setattr(requests, "post", fake_post)
        self.channels: list[TelegramChannel] = []
        yield
        for channel in self.channels:
            channel.close(timeout=2.0)

    def _channel(self, **kwargs) -> TelegramChannel:
        defaults = dict(
            bot_token="token", channel_id="chat", cloudflare_url="https://example.test",
        )
        defaults.update(kwargs)
        channel = TelegramChannel(**defaults)
        self.channels.append(channel)
        return channel

    def test_missing_configuration_sends_nothing_and_starts_no_thread(self) -> None:
        channel = TelegramChannel(bot_token="", channel_id="")
        self.channels.append(channel)

        assert channel.enabled is False
        channel.handle(Event.signal("NG-10.26", side="BUY", quantity=1, entry=1, stop=2))

        assert self.posts == []

    def test_subscribes_to_nine_lifecycle_types_by_default(self) -> None:
        assert TelegramChannel.supported_types == TRADING_EVENT_TYPES

    def test_message_is_delivered_to_http_api(self) -> None:
        channel = self._channel()
        channel.handle(Event.signal("NG-10.26", side="BUY", quantity=1, entry=1, stop=2))
        channel.close(timeout=2.0)

        assert [post["url"] for post in self.posts] == ["https://example.test/bottoken/sendMessage"]
        assert "В работе, ждёт подтверждения" in self.posts[0]["text"]

    def test_event_outside_supported_types_is_not_sent(self) -> None:
        channel = self._channel()
        channel.handle(Event.heartbeat(tick_count=1, error_count=0))
        channel.close(timeout=2.0)

        assert self.posts == []

    def test_rate_limit_is_logged_without_retry(self, monkeypatch, caplog) -> None:
        attempts: list[str] = []

        def fake_post(url, data, timeout):
            attempts.append(url)
            return _FakeResponse(429, "Too Many Requests", {"parameters": {"retry_after": 12}})

        monkeypatch.setattr(requests, "post", fake_post)
        channel = self._channel()
        channel.handle(Event.signal("NG-10.26", side="BUY", quantity=1, entry=1, stop=2))
        channel.close(timeout=2.0)

        assert len(attempts) == 1
        assert "retry_after=12" in caplog.text

    def test_network_exception_is_logged_and_worker_survives(self, monkeypatch, caplog) -> None:
        sent: list[str] = []
        attempts: list[int] = []

        def flaky_post(url, data, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise requests.ConnectionError("сеть недоступна")
            sent.append(data["text"])
            return _FakeResponse(body={"ok": True, "result": {"message_id": len(attempts)}})

        monkeypatch.setattr(requests, "post", flaky_post)
        channel = self._channel()
        for quantity in (1, 2, 3):
            channel.handle(Event.signal("NG-10.26", side="BUY", quantity=quantity, entry=1, stop=2))
        channel.close(timeout=2.0)

        assert len(attempts) == 3
        assert len(sent) == 2
        assert "ConnectionError" in caplog.text

    def test_queue_drops_oldest_when_full(self) -> None:
        channel = TelegramChannel(
            bot_token="", channel_id="", cloudflare_url="", queue_size=2,
        )
        for index in range(4):
            channel._enqueue(f"сообщение {index}")

        assert channel._queue.qsize() <= 2

    def test_dropped_messages_are_counted_in_warning(self, caplog) -> None:
        channel = TelegramChannel(
            bot_token="", channel_id="", cloudflare_url="", queue_size=2,
        )
        for index in range(4):
            channel._enqueue(f"сообщение {index}")

        assert channel._dropped == 2
        assert "отброшено 1 сообщение" in caplog.text
        assert "2 сообщений" not in caplog.text

    def test_close_warns_when_queue_does_not_drain(self, caplog) -> None:
        channel = self._channel()

        def slow_post(url, data, timeout):
            time.sleep(0.3)
            return _FakeResponse()

        monkey = pytest.MonkeyPatch()
        monkey.setattr(requests, "post", slow_post)
        try:
            for _ in range(3):
                channel._enqueue("долгое сообщение")
            channel.close(timeout=0.01)
        finally:
            channel._worker.join(timeout=1)
            monkey.undo()

        assert "не завершился за" in caplog.text

    def test_worker_thread_is_daemon(self) -> None:
        channel = self._channel()

        assert channel._worker.daemon is True

    def test_close_is_idempotent(self) -> None:
        channel = self._channel()
        channel.close(timeout=2.0)
        channel.close(timeout=2.0)

        assert channel._closed is True
