from pathlib import Path

from app.routers.gateway_config import _effective_mode


SOURCE = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
    encoding="utf-8"
)


def test_live_alias_is_never_a_real_live_gateway_mode():
    profile = type("Profile", (), {"allow_real_live": True, "allow_demo_live": True})()
    assert _effective_mode("LIVE", profile, real_live_mode_allowed=True, real_live_armed=True) == "PAPER"


def test_real_live_needs_both_explicit_phase_gates():
    profile = type("Profile", (), {"allow_real_live": True, "allow_demo_live": True})()
    assert _effective_mode("REAL_LIVE", profile, real_live_mode_allowed=False, real_live_armed=True) == "PAPER"
    assert _effective_mode("REAL_LIVE", profile, real_live_mode_allowed=True, real_live_armed=False) == "PAPER"


def test_gateway_refreshes_demo_account_before_orders_and_fails_closed():
    check = SOURCE.split("private string CheckModeGates", 1)[1].split("private decimal GetSellableQty", 1)[0]
    assert "VerifyDemoAccountFresh()" in check
    verification = SOURCE.split("private bool VerifyDemoAccountFresh", 1)[1].split("private void IncrementDailyTradeCount", 1)[0]
    assert "GetTradeUser()" in verification
    assert "AccountVerificationMaxAgeSeconds" in verification
    assert "_lastAccountVerificationUtc = DateTime.MinValue" in verification
