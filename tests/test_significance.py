"""Önem dedektörü testleri (v2 Faz 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.significance import (
    SignificanceDetector,
    SymbolObservation,
    build_observation,
)


def _obs(**overrides) -> SymbolObservation:
    defaults = dict(
        symbol="THYAO",
        observed_at=datetime.now(UTC),
        last_price=100.0,
        consensus="NEUTRAL",
        rsi=50.0,
        macd_hist=0.5,
        adx=20.0,
        most_signal="LONG",
        depth_imbalance=1.0,
        news_fp=("haber-1",),
        kap_fp=(),
        position_qty=0.0,
        active_stop=None,
    )
    defaults.update(overrides)
    return SymbolObservation(**defaults)


def _detector_with_baseline(**baseline_overrides) -> SignificanceDetector:
    detector = SignificanceDetector()
    detector.record_ai_evaluation(_obs(**baseline_overrides))
    return detector


def test_no_baseline_is_significant():
    detector = SignificanceDetector()
    result = detector.assess(_obs())
    assert result.significant is True
    assert result.triggers == ("NO_BASELINE",)


def test_unchanged_observation_is_not_significant():
    detector = _detector_with_baseline()
    result = detector.assess(_obs())
    assert result.significant is False
    assert result.triggers == ()


def test_price_move_over_threshold_triggers():
    detector = _detector_with_baseline()
    result = detector.assess(_obs(last_price=102.0))  # %2 >= %1.5
    assert result.significant is True
    assert any(t.startswith("PRICE_MOVE") for t in result.triggers)


def test_price_move_under_threshold_does_not_trigger():
    detector = _detector_with_baseline()
    result = detector.assess(_obs(last_price=101.0))  # %1 < %1.5
    assert result.significant is False


def test_held_position_uses_tighter_threshold():
    detector = _detector_with_baseline(position_qty=10.0)
    # %1.2 hareket: normalde eşiğin altında, pozisyonda (1.5*2/3=%1.0) üstünde.
    result = detector.assess(_obs(last_price=101.2, position_qty=10.0))
    assert result.significant is True


def test_consensus_flip_triggers():
    detector = _detector_with_baseline(consensus="NEUTRAL")
    result = detector.assess(_obs(consensus="BUY"))
    assert any(t.startswith("CONSENSUS_FLIP") for t in result.triggers)


def test_news_fingerprint_change_triggers():
    detector = _detector_with_baseline(news_fp=("haber-1",))
    result = detector.assess(_obs(news_fp=("haber-1", "haber-2")))
    assert "NEWS_CHANGED" in result.triggers


def test_kap_fingerprint_change_triggers():
    detector = _detector_with_baseline(kap_fp=())
    result = detector.assess(_obs(kap_fp=("kap-1",)))
    assert "KAP_CHANGED" in result.triggers


def test_rsi_cross_70_triggers():
    detector = _detector_with_baseline(rsi=65.0)
    result = detector.assess(_obs(rsi=72.0))
    assert "RSI_CROSS_70" in result.triggers


def test_rsi_cross_30_triggers():
    detector = _detector_with_baseline(rsi=35.0)
    result = detector.assess(_obs(rsi=28.0))
    assert "RSI_CROSS_30" in result.triggers


def test_macd_histogram_sign_flip_triggers():
    detector = _detector_with_baseline(macd_hist=0.4)
    result = detector.assess(_obs(macd_hist=-0.2))
    assert "MACD_HIST_SIGN_FLIP" in result.triggers


def test_adx_cross_25_triggers():
    detector = _detector_with_baseline(adx=20.0)
    result = detector.assess(_obs(adx=28.0))
    assert "ADX_CROSS_25" in result.triggers


def test_most_signal_flip_triggers():
    detector = _detector_with_baseline(most_signal="LONG")
    result = detector.assess(_obs(most_signal="SHORT"))
    assert any(t.startswith("MOST_FLIP") for t in result.triggers)


def test_depth_imbalance_leaving_band_triggers():
    detector = _detector_with_baseline(depth_imbalance=1.2)
    result = detector.assess(_obs(depth_imbalance=2.5))
    assert "DEPTH_IMBALANCE_SHIFT" in result.triggers


def test_depth_imbalance_side_flip_triggers():
    detector = _detector_with_baseline(depth_imbalance=1.4)
    result = detector.assess(_obs(depth_imbalance=0.7))
    assert "DEPTH_IMBALANCE_SHIFT" in result.triggers


def test_near_stop_triggers_for_held_position():
    detector = _detector_with_baseline(
        position_qty=10.0, active_stop=99.5, last_price=100.0
    )
    result = detector.assess(
        _obs(position_qty=10.0, active_stop=99.5, last_price=100.0)
    )
    assert "NEAR_STOP" in result.triggers


def test_stale_baseline_backstop_triggers():
    detector = SignificanceDetector()
    old = _obs(observed_at=datetime.now(UTC) - timedelta(hours=5))
    detector.record_ai_evaluation(old)
    result = detector.assess(_obs())
    assert "STALE_BASELINE_4H" in result.triggers


def test_missing_fields_never_trigger_alone():
    """Baseline'da veya gözlemde eksik alan (None) tetikleyici üretmez —
    veri yokluğu değişim kanıtı değildir."""
    detector = _detector_with_baseline(
        rsi=None, adx=None, macd_hist=None, most_signal=None, depth_imbalance=None
    )
    result = detector.assess(
        _obs(rsi=55.0, adx=30.0, macd_hist=1.0, most_signal="SHORT", depth_imbalance=3.0)
    )
    assert result.significant is False


def test_skipped_scan_does_not_move_baseline():
    detector = _detector_with_baseline(last_price=100.0)
    # %1'lik iki adım: baseline güncellenmediği için ikinci adımda kümülatif
    # %2 hareket tetikler.
    first = detector.assess(_obs(last_price=101.0))
    assert first.significant is False
    second = detector.assess(_obs(last_price=102.0))
    assert second.significant is True


def test_build_observation_reads_snapshot_payload():
    payload = {
        "lastPrice": 71.5,
        "rsi": 55.0,
        "macd": 1.2,
        "macdSignal": 1.0,
        "bidVolume": 3000.0,
        "askVolume": 1500.0,
        "technicalFeatures": {
            "indicatorConsensus": "BUY",
            "adx": 27.0,
            "mostSignal": "long",
        },
    }
    obs = build_observation(
        "thyao",
        payload,
        position_qty=5.0,
        active_stop=68.0,
        news_fp=("n1",),
        kap_fp=("k1",),
    )
    assert obs.symbol == "THYAO"
    assert obs.last_price == 71.5
    assert obs.consensus == "BUY"
    assert obs.adx == 27.0
    assert obs.most_signal == "LONG"
    assert round(obs.macd_hist, 4) == 0.2
    assert obs.depth_imbalance == 2.0
    assert obs.position_qty == 5.0
    assert obs.active_stop == 68.0
