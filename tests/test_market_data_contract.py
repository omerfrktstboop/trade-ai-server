from __future__ import annotations

from app.models.signal import SignalMode
from app.services.evaluator import build_payload, snapshot_to_signal_request
from app.services.market_data_contract import (
    canonical_period,
    normalize_snapshot_payload,
)


def test_v2_contract_keeps_session_turnover_separate_from_bar_volume():
    payload = normalize_snapshot_payload(
        {
            "requestedTimeframe": "1h",
            "actualBarPeriod": "MIN5",
            "actualBarPeriodSeconds": 300,
            "barPeriodSource": "BarDataEventArgs.PeriodInfo",
            "totalVol": 1_917_814_254.1,
            "totalVolSemantic": "CUMULATIVE_SESSION_TURNOVER_TL",
            "barVolume": 123_456,
            "barVolumeUnit": "UNITS",
        }
    )

    assert payload["sessionTurnoverTl"] == 1_917_814_254.1
    assert payload["barVolume"] == 123_456
    assert payload["volume"] == 123_456
    assert payload["timeframe"] == "MIN5"
    assert payload["timeframeMismatch"] is True


def test_known_turnover_is_never_reinterpreted_as_bar_volume():
    payload = normalize_snapshot_payload(
        {
            "timeframe": "1h",
            "volume": 1_917_814_254.1,
            "volumeSemantic": "CUMULATIVE_SESSION_TURNOVER_TL",
        }
    )

    assert payload["barVolume"] is None
    assert payload["volume"] == 0


def test_legacy_ambiguous_volume_remains_compatible_but_unreliable():
    payload = normalize_snapshot_payload({"timeframe": "Min5", "volume": 5000})

    assert payload["barVolume"] == 5000
    assert payload["barVolumeReliable"] is False
    assert payload["volumeSemantic"] == "LEGACY_AMBIGUOUS"


def test_period_aliases_compare_without_false_mismatch():
    assert canonical_period("1h") == canonical_period("Min60") == "MIN60"
    payload = normalize_snapshot_payload(
        {"requestedTimeframe": "1h", "actualBarPeriod": "Min60"}
    )
    assert payload["timeframeMismatch"] is False


def test_signal_and_ai_payload_expose_explicit_semantics():
    request = snapshot_to_signal_request(
        "KCHOL",
        {
            "instrumentType": "EQUITY",
            "requestedTimeframe": "1h",
            "actualBarPeriod": "MIN5",
            "actualBarPeriodSeconds": 300,
            "barPeriodSource": "BarDataEventArgs.PeriodInfo",
            "lastPrice": 191.4,
            "open": 190,
            "high": 192,
            "low": 189,
            "barVolume": 250_000,
            "sessionTurnoverTl": 1_917_814_254.1,
            "totalVol": 1_917_814_254.1,
        },
        request_id="contract-1",
        mode=SignalMode.DEMO_LIVE,
    )

    ai_payload = build_payload(request)
    assert request.timeframe == "MIN5"
    assert request.volume == 250_000
    assert ai_payload["barVolume"] == 250_000
    assert ai_payload["sessionTurnoverTl"] == 1_917_814_254.1
    assert ai_payload["timeframeMismatch"] is True


def test_gateway_source_uses_official_bar_fields_and_type_aware_depth():
    source = open("matriks/TradeAiGateway.cs", encoding="utf-8-sig").read()

    assert "barData.BarData.Dtime" in source
    assert "barData.BarData.Volume" in source
    assert "barData.BarDataIndex" in source
    assert "barData.IsNewBar" in source
    assert "barData.LastTickTime" in source
    assert 'payload["sessionTurnoverTl"]' in source
    assert 'payload["actualBarPeriod"]' in source
    assert "IsEquitySymbol(normalized)" in source
    assert "MarketDataDiagnosticsEnabled" in source
