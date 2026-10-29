"""Unit-тесты каналов: консоль, порт канала, Telegram-очередь."""

import io
import os
import time
from functools import partial
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.events import EventBus
from src.events.event import Event
from src.events.types import TRADING_EVENT_TYPES, EventType
from src.notifier.channel import Channel
from src.notifier.console import ConsoleChannel, _supports_color
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
    def test_shared_api_limit_is_one_line_per_episode(self) -> None:
        clock = [datetime(2026, 10, 13, 11, 30, tzinfo=timezone.utc)]
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: clock[0])

        def status(instrument: str, state: str) -> Event:
            return Event.market_data_status(instrument, timeframe="15m", session="open",
                data_state=state, observed_at=clock[0].replace(tzinfo=None),
                reason="Ожидание сброса общего лимита API" if state == "rate_limited" else "")

        for instrument in ("SBER", "PHOR", "VTBR"):
            channel.handle(status(instrument, "rate_limited"))
        assert stream.getvalue() == "14:30  СИСТЕМА       Лимит API · ожидание сброса\n"

        clock[0] += timedelta(minutes=2)
        channel.handle(status("SBER", "current"))
        assert stream.getvalue().count("Лимит API") == 1
        channel.handle(status("SBER", "rate_limited"))
        assert stream.getvalue().count("Лимит API") == 2

    def test_api_limit_inside_batch_is_one_system_line_next_to_grouped_states(self, monkeypatch) -> None:
        monkeypatch.setattr("src.notifier.console.shutil.get_terminal_size", lambda **_: os.terminal_size((120, 24)))
        observed = datetime(2026, 10, 13, 13, 15)
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)
        channel.handle(Event.tick_started(tick_id="cycle"))
        for instrument in ("SBER", "PHOR", "VTBR"):
            channel.handle(Event.market_data_status(instrument, timeframe="15m", session="open",
                data_state="rate_limited", observed_at=observed, reason="Ожидание сброса общего лимита API"))
        channel.handle(Event.market_data_status("MOEX", timeframe="15m", session="open",
            data_state="warming_up", observed_at=observed, available_bars=12, required_bars=60))
        assert stream.getvalue() == ""
        channel.handle(Event.tick_finished(tick_id="cycle", completed=True))
        assert stream.getvalue() == (
            "16:15  СИСТЕМА       Лимит API · ожидание сброса\n"
            "\n"
            "16:15  ПРОГРЕВ           1 пара\n"
            "                        MOEX · 15м · 12/60 свечей\n"
        )

    def test_close_flushes_unfinished_status_batch(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)
        channel.handle(Event.tick_started(tick_id="interrupted"))
        channel.handle(Event.market_data_status("SBER", timeframe="15m", session="open",
            data_state="delayed", observed_at=datetime(2026, 10, 13, 13, 15),
            expected_bar=datetime(2026, 10, 13, 13, 0)))
        assert stream.getvalue() == ""
        channel.close()
        assert "ЗАДЕРЖКА ДАННЫХ  1 пара" in stream.getvalue()
        assert "SBER · 15м · не получена свеча 16:00" in stream.getvalue()

    def test_startup_overview_between_visible_events_leaves_no_trace(self) -> None:
        def run(with_overview: bool) -> str:
            stream = io.StringIO()
            channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))
            channel.handle(Event.heartbeat(tick_count=60, error_count=0))
            if with_overview:
                for outcome in ("HOLD", "BUY", "SELL", "FILTERED", "ERROR"):
                    channel.handle(Event.startup_analysis("SBER", published_at=datetime(2026, 10, 13, 12, 0),
                        results=({"outcome": outcome},), available_bars=1, required_bars=60,
                        coverage_complete=False, reason="прогрев"))
            channel.handle(Event.tick_started(tick_id="t"))
            channel.handle(Event.decision("SBER", outcome="no_signal", side="HOLD", timeframe="15m"))
            channel.handle(Event.tick_finished(tick_id="t", completed=True))
            return stream.getvalue()

        assert run(with_overview=True) == run(with_overview=False)
        assert "проверено 1 пара" in run(with_overview=True)

    def test_secondary_order_joins_trade_but_standalone_order_has_time(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 13, 12, 37, tzinfo=timezone.utc))
        channel.handle(Event.broker_event(EventType.ORDER_REJECTED, trade_id="other", instrument="SBER",
            side="BUY", reason="trade-not-open"))
        channel.handle(Event.broker_event(EventType.TRADE_CLOSED, trade_id="br", instrument="BR–11.26",
            quantity=2, price="103.57", quantity_remaining=0))
        channel.handle(Event.broker_event(EventType.ORDER_REJECTED, trade_id="br", instrument="BR–11.26",
            side="SELL", reason="trade-not-open"))
        lines = stream.getvalue().splitlines()
        assert lines[0].startswith("15:37  ОРДЕР         SBER · покупка отклонена")
        assert lines[1] == ""
        assert lines[2].startswith("15:37  СДЕЛКА        BR–11.26")
        assert lines[3].startswith("       ОРДЕР         BR–11.26 · продажа отклонена")

    def test_direct_print_outside_channel_is_not_intercepted(self, capsys) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)
        print("прямой вывод")
        channel.handle(Event.heartbeat(tick_count=1, error_count=0))
        print("ещё строка")
        assert capsys.readouterr().out == "прямой вывод\nещё строка\n"
        assert "СИСТЕМА" in stream.getvalue() and "прямой вывод" not in stream.getvalue()

    def test_repeated_pair_warning_is_throttled_but_recovery_resets_it(self) -> None:
        clock = [datetime(2026, 10, 13, 11, 30, tzinfo=timezone.utc)]
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: clock[0])

        def status(state: str) -> Event:
            return Event.market_data_status("SBER", timeframe="15m", session="open",
                data_state=state, observed_at=clock[0].replace(tzinfo=None),
                reason="Нет свечи" if state == "delayed" else "")

        channel.handle(status("delayed"))
        clock[0] += timedelta(minutes=1)
        channel.handle(status("delayed"))
        assert stream.getvalue().count("ЗАДЕРЖКА ДАННЫХ") == 1
        clock[0] += timedelta(minutes=15)
        channel.handle(status("delayed"))
        assert stream.getvalue().count("ЗАДЕРЖКА ДАННЫХ") == 2
        channel.handle(status("current"))
        channel.handle(status("delayed"))
        assert stream.getvalue().count("ЗАДЕРЖКА ДАННЫХ") == 3

    def test_color_capability_respects_terminal_and_environment(self, monkeypatch) -> None:
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        terminal = Terminal()
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        assert _supports_color(terminal, platform_name="posix")
        assert not _supports_color(io.StringIO(), platform_name="posix")
        assert not _supports_color(terminal, platform_name="nt")
        monkeypatch.setenv("NO_COLOR", "")
        assert not _supports_color(terminal, platform_name="posix")
        monkeypatch.delenv("NO_COLOR")
        monkeypatch.setenv("TERM", "dumb")
        assert not _supports_color(terminal, platform_name="posix")

    def test_terminal_color_and_pipe_plain_text_match(self, monkeypatch) -> None:
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        monkeypatch.delenv("NO_COLOR", raising=False)
        # На Windows ANSI допустим только в настоящей консоли; здесь проверяется POSIX-терминал.
        monkeypatch.setattr("src.notifier.console._supports_color",
                            partial(_supports_color, platform_name="posix"))
        monkeypatch.setenv("TERM", "xterm-256color")
        def now() -> datetime:
            return datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc)
        event = Event.decision("SBER", outcome="signal_sell", side="SELL", price=100)
        terminal = Terminal()
        plain = io.StringIO()
        ConsoleChannel(stream=terminal, now=now).handle(event)
        ConsoleChannel(stream=plain, now=now).handle(event)
        assert "\x1b[35m" in terminal.getvalue()
        assert terminal.getvalue().endswith("\x1b[35mПРОДАЖА\x1b[0m · цена 100.000\n")
        assert terminal.getvalue().replace("\x1b[35m", "").replace("\x1b[0m", "") == plain.getvalue()

    def test_quiet_scan_is_muted_only_in_terminal(self, monkeypatch) -> None:
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        monkeypatch.delenv("NO_COLOR", raising=False)
        # На Windows ANSI допустим только в настоящей консоли; здесь проверяется POSIX-терминал.
        monkeypatch.setattr("src.notifier.console._supports_color",
                            partial(_supports_color, platform_name="posix"))
        monkeypatch.setenv("TERM", "xterm")
        stream = Terminal()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))
        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("SBER", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))
        assert stream.getvalue() == "\x1b[90m10:15  СКАНИРОВАНИЕ  Сигналов нет · проверено 1 пара\x1b[0m\n"

    def test_prints_rendered_text(self, capsys) -> None:
        ConsoleChannel(now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc)).handle(
            Event.heartbeat(tick_count=1, error_count=0))

        assert capsys.readouterr().out == "10:15  СИСТЕМА       1 такт работы · 0 ошибок\n"

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

    def test_closed_stream_disables_channel_without_error(self) -> None:
        stream = io.StringIO()
        stream.close()
        channel = ConsoleChannel(stream=stream)
        channel.handle(Event.heartbeat(tick_count=1, error_count=0))
        channel.handle(Event.heartbeat(tick_count=2, error_count=0))
        assert channel._disabled

    def test_whole_multiline_block_uses_one_write(self) -> None:
        class CountingStream(io.StringIO):
            writes = 0

            def write(self, value: str) -> int:
                self.writes += 1
                return super().write(value)

        stream = CountingStream()
        channel = ConsoleChannel(stream=stream)
        channel.handle(Event.error(operation="анализ сделки"))
        assert stream.writes == 1
        assert stream.getvalue().count("\n") == 2

    def test_quiet_tick_prints_one_summary_through_bus(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(
            stream=stream,
            supported_types={EventType.DECISION, EventType.ERROR},
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

        assert stream.getvalue() == "10:15  СКАНИРОВАНИЕ  Сигналов нет · проверено 4 пары\n"

    def test_printed_event_suppresses_quiet_tick_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.decision("SBER", outcome="signal_buy", side="BUY", price=100))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "10:15  СИГНАЛ        SBER · ПОКУПКА · цена 100.000\n"

    def test_decision_plan_and_rejection_keep_publication_order(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))
        channel.handle(Event.decision("SBER", outcome="signal_buy", side="BUY", price=100))
        channel.handle(Event.signal("SBER", side="BUY", quantity=1, entry=100, stop=96))
        channel.handle(Event.rejected("SBER", reason="лимит риска", side="BUY"))

        text = stream.getvalue()
        assert text.index("СИГНАЛ") < text.index("ПЛАН") < text.index("ОТКЛОНЕНО")
        assert text.count("10:15  ") == 3
        assert text.count("SBER") == 3

    def test_filtered_hold_is_printed_and_suppresses_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream)

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.decision("SBER", outcome="filtered", side="HOLD", filtered_out=True))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue().endswith("ОТКЛОНЕНО ФИЛЬТРОМ  SBER\n")

    def test_holds_for_different_timeframes_and_profiles_count_separately(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        for timeframe, profile in (("5m", "raw"), ("15m", "raw"), ("15m", "basic_levels")):
            channel.handle(Event.decision(
                "GAZP", outcome="no_signal", side="HOLD", timeframe=timeframe, filter_profile=profile,
            ))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "10:15  СКАНИРОВАНИЕ  Сигналов нет · проверено 2 пары\n"

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
        for index in range(21):
            channel.handle(Event.decision(f"GAZP{index}", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "01:15  СКАНИРОВАНИЕ  Сигналов нет · проверено 21 пара\n"

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

        assert stream.getvalue().endswith("10:15  СКАНИРОВАНИЕ  Сигналов нет · проверено 1 пара\n")

    def test_event_without_text_does_not_suppress_summary(self) -> None:
        stream = io.StringIO()
        channel = ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 8, 7, 15, tzinfo=timezone.utc))

        channel.handle(Event.tick_started(tick_id="one"))
        channel.handle(Event.rate_limited(source="tinkoff"))
        channel.handle(Event.decision("GAZP", outcome="no_signal", side="HOLD"))
        channel.handle(Event.tick_finished(tick_id="one", completed=True))

        assert stream.getvalue() == "10:15  СКАНИРОВАНИЕ  Сигналов нет · проверено 1 пара\n"

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


def test_console_repeats_hold_only_when_its_boundary_moves() -> None:
    clock = [datetime(2026, 10, 15, 8, 0, 5, tzinfo=timezone.utc)]
    stream = io.StringIO()
    channel = ConsoleChannel(stream=stream, now=lambda: clock[0])

    def held(through: datetime) -> Event:
        return Event.market_data_status("SBER", timeframe="15m", session="open", data_state="waiting_bar",
            observed_at=clock[0], hold={"kind": "execution_barrier", "through": through})

    channel.handle(held(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)))
    clock[0] += timedelta(minutes=1)
    channel.handle(held(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)))
    assert stream.getvalue().count("ДАННЫЕ НЕДОСТУПНЫ") == 1
    channel.handle(held(datetime(2026, 10, 15, 8, 15, tzinfo=timezone.utc)))
    assert stream.getvalue().count("ДАННЫЕ НЕДОСТУПНЫ") == 2
    assert "не подтверждена до 11:15" in stream.getvalue()
