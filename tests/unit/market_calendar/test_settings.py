import pytest

from src.data.ingestion_settings import MarketDataSettings, SettingError, candle_publication_timeout


def test_calendar_defaults_keep_old_configs_valid():
    settings = MarketDataSettings()

    assert settings.schedule_refresh_interval_secs == 3600
    assert settings.schedule_max_age_secs == 129600
    assert settings.schedule_horizon_days == 7
    assert settings.calendar_evidence is True
    assert candle_publication_timeout(settings, "1d") == 65


def test_calendar_settings_validate_evidence_and_per_timeframe_timeout():
    settings = MarketDataSettings(calendar_evidence=False, candle_publication_timeout_by_tf={"1d": 900})

    assert settings.calendar_evidence is False
    assert candle_publication_timeout(settings, "1d") == 900
    assert candle_publication_timeout(settings, "1m") == 65

    with pytest.raises(SettingError, match="schedule_max_age_secs"):
        MarketDataSettings(schedule_max_age_secs=-1)
    with pytest.raises(SettingError, match="неизвестный ТФ"):
        MarketDataSettings(candle_publication_timeout_by_tf={"2h": 30})
    with pytest.raises(SettingError, match="calendar_evidence"):
        MarketDataSettings(calendar_evidence=1)
