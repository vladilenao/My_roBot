"""Контрактные роли и сохраняемый барьер перехода; деньги ведёт reducer."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json

from src.data.timeutil import to_naive
from src.instruments.futures import catalog_digest
from src.instruments.model import instrument_key


class ContractLifecycleError(RuntimeError):
    """Прогон нельзя продолжить с незавершёнными обязательствами."""


def _stamp(moment):
    return to_naive(moment).isoformat()+"Z"


class ConstantContractLifecycle:
    automatic = False

    def __init__(self, instruments):
        self.instruments = list(instruments)

    def market_instruments(self):
        return self.instruments

    def execution_instruments(self):
        return self.instruments

    def before_tick(self, now):
        return None

    def after_execution(self, now):
        return None

    def can_increase(self, ticker, now):
        return True

    def signal_allowed(self, instrument, bar_open, origin, now):
        return True

    def execution_increase_allowed(self, ticker, bar_open):
        return True

    def retiring(self, ticker, now):
        return False

    def next_event(self, now):
        return None

    def contains(self, ticker):
        return False


class RolloverLifecycle(ConstantContractLifecycle):
    automatic = True

    def __init__(self, selection, storage, manager, cache, requirements, end, work_timeframes=None):
        super().__init__(selection.instruments)
        self.selection, self.storage, self.manager, self.cache = selection, storage, manager, cache
        self.requirements, self.end = requirements, to_naive(end).to_pydatetime()
        self.work_timeframes = work_timeframes
        self.chains = {chain[0].card.family: chain for chain in selection.chains}
        self.by_ticker = {s.card.uid: s for chain in selection.chains for s in chain}
        self.states = {}
        digest = catalog_digest(selection.catalog)
        for family in self.chains:
            row = storage.connection.execute("SELECT catalog_digest,payload_json FROM rollover_states WHERE family=?", (family,)).fetchone()
            if row and row[0] != digest:
                raise ContractLifecycleError("Каталог существующего прогона отличается от сохранённого")
            self.states[family] = json.loads(row[1]) if row else {"index": 0, "phase": "WAITING_DATA", "activation_at": None, "transition_id": None}
        self.digest = digest

    def _save(self, family, transition=None):
        try:
            with self.storage.transaction() as connection:
                connection.execute("INSERT INTO rollover_states VALUES (?,?,?) ON CONFLICT(family) DO UPDATE SET payload_json=excluded.payload_json",
                    (family, self.digest, json.dumps(self.states[family], sort_keys=True)))
                if transition is not None:
                    connection.execute("INSERT INTO rollover_transitions VALUES (?,?,?) ON CONFLICT(transition_id) DO UPDATE SET payload_json=excluded.payload_json",
                        (transition["transition_id"], family, json.dumps(transition, sort_keys=True)))
        except BaseException:
            row = self.storage.connection.execute("SELECT payload_json FROM rollover_states WHERE family=?", (family,)).fetchone()
            self.states[family] = json.loads(row[0]) if row else {"index": 0, "phase": "WAITING_DATA", "activation_at": None, "transition_id": None}
            raise

    def _transition(self, state):
        row = self.storage.connection.execute("SELECT payload_json FROM rollover_transitions WHERE transition_id=?", (state["transition_id"],)).fetchone()
        return json.loads(row[0]) if row else None

    def _begin(self, family, now, skipped=False):
        state, chain = self.states[family], self.chains[family]
        old, new = chain[state["index"]], chain[state["index"]+1]
        identity = sha256(f"{family}:{old.card.uid}:{new.card.uid}:{old.cutoff}".encode()).hexdigest()
        record = {"transition_id": identity, "family": family, "old_contract": old.card.short_name,
                  "new_contract": new.card.short_name, "cutoff_at": _stamp(old.cutoff),
                  "draining_at": _stamp(now), "old_flat_at": None, "activation_at": None,
                  "status": "skipped" if skipped else "draining"}
        state.update(phase="DRAINING", transition_id=identity, activation_at=None)
        self._save(family, record)

    def market_instruments(self):
        result = list(self.selection.fixed)
        for family, state in self.states.items():
            chain = self.chains[family]
            result.append(chain[state["index"]].card.instrument)
            if state["phase"] == "DRAINING" and state["index"]+1 < len(chain):
                result.append(chain[state["index"]+1].card.instrument)
        return result

    def configure_cache(self):
        instruments = self.market_instruments()
        pairs = None if self.work_timeframes is None else {
            (instrument_key(instrument), tf) for instrument in instruments for tf in self.work_timeframes(instrument)}
        self.cache.set_active_instruments(instruments, next_event=self.next_event, active_pairs=pairs)

    def execution_instruments(self):
        # Только контракт с реальными обязательствами, не вся прогретая цепочка.
        return list(self.selection.fixed)+[s.card.instrument for s in self.by_ticker.values()
            if self.manager.contract_has_obligations(s.card.uid)]

    def before_tick(self, now):
        now = to_naive(now).to_pydatetime()
        for family, state in self.states.items():
            part = self.chains[family][state["index"]]
            if state["phase"] == "ACTIVE" and part.cutoff <= now and part.cutoff < self.end:
                self._begin(family, now)
        self.configure_cache()
        for family, state in self.states.items():
            chain = self.chains[family]
            if state["phase"] == "ACTIVE" and state["index"]+1 < len(chain):
                next_card = chain[state["index"]+1].card
                if next_card.first_trade_date < now:
                    for tf, count in self.requirements(family).items():
                        self.cache.prepare_history(next_card.instrument, tf, count, next_card.first_trade_date)

    def after_execution(self, now):
        now = to_naive(now).to_pydatetime()
        for family, state in self.states.items():
            chain = self.chains[family]
            part = chain[state["index"]]
            if state["phase"] == "DRAINING":
                record = self._transition(state)
                if now >= part.deadline and self.manager.contract_has_obligations(part.card.uid):
                    record["status"] = "incomplete"
                    self._save(family, record)
                    raise ContractLifecycleError(f"{part.card.short_name}: обязательства не завершены до границы торговли")
                frame = self.cache.frame_for(part.card.instrument, "1m")
                close = None if frame.empty else frame["close"].iloc[-1]
                commands = self.manager.drain_contract(part.card.uid, market_close=close)
                self.manager.dispatch(now)
                self.manager.consume_queued_broker_events()
                with self.storage.transaction() as connection:
                    for action in commands:
                        connection.execute("INSERT OR IGNORE INTO rollover_commands VALUES (?,?)", (state["transition_id"], action.command_id))
                    related = connection.execute(
                        "SELECT o.command_id FROM orders o JOIN trades t USING(trade_id) "
                        "WHERE t.instrument_id=? AND (o.action_type IN ('CLOSE','REDUCE','STOP') OR o.action_type LIKE 'TARGET:%') AND "
                        "(o.status IN ('PENDING','ACK','PARTIAL') OR EXISTS(SELECT 1 FROM fills f "
                        "WHERE f.command_id=o.command_id AND julianday(f.executed_at)>=julianday(?)))",
                        (part.card.uid, _stamp(part.cutoff))).fetchall()
                    for (command_id,) in related:
                        connection.execute("INSERT OR IGNORE INTO rollover_commands VALUES (?,?)", (state["transition_id"], command_id))
                if self.manager.contract_has_obligations(part.card.uid):
                    continue
                record["old_flat_at"] = _stamp(now)
                record["status"] = "waiting_data"
                state.update(index=state["index"]+1, phase="WAITING_DATA")
                self._save(family, record)
                part = chain[state["index"]]
            while state["phase"] == "WAITING_DATA" and part.cutoff <= now and part.cutoff < self.end:
                previous = self._transition(state) if state["transition_id"] else None
                if previous:
                    previous["status"] = "skipped"
                    self._save(family, previous)
                self._begin(family, now, skipped=True)
                record = self._transition(state)
                record["old_flat_at"] = _stamp(now)
                state.update(index=state["index"]+1, phase="WAITING_DATA")
                self._save(family, record)
                part = chain[state["index"]]
            if state["phase"] == "WAITING_DATA" and now < part.cutoff:
                ready = all(self.cache.prepare_history(part.card.instrument, tf, count, part.card.first_trade_date)
                            for tf, count in self.requirements(family).items())
                base = self.cache.frame_for(part.card.instrument, "1m")
                fresh = not base.empty and to_naive(base["datetime"].iloc[-1])+timedelta(minutes=1) == now
                if ready and fresh:
                    initial = state["index"] == 0 and state["transition_id"] is None
                    state.update(phase="ACTIVE", activation_at=_stamp(part.start if initial else now))
                    record = self._transition(state) if state["transition_id"] else None
                    if record:
                        record.update(status="active", activation_at=_stamp(now))
                    self._save(family, record)
        self.configure_cache()

    def can_increase(self, ticker, now):
        part = self.by_ticker.get(ticker)
        if part is None:
            return True
        state = self.states[part.card.family]
        current = self.chains[part.card.family][state["index"]]
        return state["phase"] == "ACTIVE" and current.card.uid == ticker and to_naive(now) < part.cutoff

    def contains(self, ticker):
        return ticker in self.by_ticker

    def retiring(self, ticker, now):
        part = self.by_ticker.get(ticker)
        return part is not None and part.cutoff < self.end and to_naive(now) >= part.cutoff

    def signal_allowed(self, instrument, bar_open, origin, now):
        if instrument_key(instrument) not in self.by_ticker:
            return True
        if not self.can_increase(instrument_key(instrument), now):
            return False
        activation = to_naive(self.states[self.by_ticker[instrument_key(instrument)].card.family]["activation_at"])
        return to_naive(bar_open) >= activation and (origin is None or to_naive(origin) >= activation)

    def execution_increase_allowed(self, ticker, bar_open):
        part = self.by_ticker.get(ticker)
        return part is None or (part.card.first_trade_date <= to_naive(bar_open)
            and to_naive(bar_open)+timedelta(minutes=1) <= part.cutoff
            and to_naive(bar_open) < part.cutoff)

    def next_event(self, now):
        events = []
        for family, state in self.states.items():
            part = self.chains[family][state["index"]]
            event = part.deadline if state["phase"] == "DRAINING" else part.cutoff
            if to_naive(now) < event < self.end:
                events.append(event)
        return min(events) if events else None

    def finish(self, now):
        for family, state in self.states.items():
            if state["transition_id"]:
                record = self._transition(state)
                if record["status"] in {"draining", "waiting_data"}:
                    record.update(status="incomplete", stopped_at=_stamp(now))
                    self._save(family, record)
