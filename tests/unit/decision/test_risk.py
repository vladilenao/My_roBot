from src.decision import RiskManager
from src.market_context.models import MarketContext, TrendDirection, TrendResult
from src.strategies.contracts import Decision, SignalType


def test_legacy_risk_manager_does_not_enrich_entry_event():
    decision = Decision(SignalType.BUY, 100.0, event_id="entry-1")
    context = MarketContext(TrendResult(TrendDirection.UP, 0.8), [], 100.0)

    assert RiskManager().apply(decision, context) is decision
    assert not hasattr(decision, "stop_loss")
