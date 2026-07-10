from app.services.block_reason_classifier import classify_block_reason


def test_classifies_common_risk_blocks():
    assert classify_block_reason("Confidence below minimum") == "CONFIDENCE_LOW"
    assert classify_block_reason("Daily trade limit reached") == "DAILY_LIMIT"
    assert classify_block_reason("Kill switch enabled") == "KILL_SWITCH"


def test_classifies_gateway_and_unknown():
    assert classify_block_reason("Gateway rejected order") == "GATEWAY_REJECTED"
    assert classify_block_reason("unrelated text") == "UNKNOWN"
