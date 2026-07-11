from app.services.kap_service import classify_kap


def test_kap_classifier_marks_brut_takas_as_blocking():
    assert classify_kap("Brüt takas tedbiri", None) == ("BRUT_TAKAS", "BLOCKING")


def test_kap_classifier_keeps_dividend_low_risk():
    assert classify_kap("Temettü dağıtım kararı", None) == ("DIVIDEND", "LOW")


def test_kap_classifier_marks_share_sale_as_medium_risk():
    assert classify_kap("Ortak pay satışı açıklaması", None) == ("SHARE_SALE", "MEDIUM")
