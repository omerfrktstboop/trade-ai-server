from app.services.watchlist_quality import calculate_quality


def test_quality_rewards_positive_liquid_balanced_candidate():
    score = calculate_quality({"changePct": 3, "volume": 1_000_000_000}, 1.0)
    assert score["quality"] >= 60


def test_quality_penalizes_extreme_move_and_thin_volume():
    score = calculate_quality({"changePct": 10, "volume": 1_000_000}, 5.0)
    assert score["quality"] < 60
