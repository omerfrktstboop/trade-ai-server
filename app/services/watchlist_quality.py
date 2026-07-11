from __future__ import annotations
from typing import Any
def calculate_quality(item: dict[str, Any], ask_bid_ratio: float | None) -> dict[str, float]:
    change=float(item.get("changePct") or 0); volume=float(item.get("volume") or 0)
    momentum=max(0,min(100, 70 + change*4 if 0 < change < 8 else 35))
    volume_score=max(0,min(100, volume / 1_000_000 * 10))
    depth=50 if ask_bid_ratio is None else max(0,min(100, 80 - max(0,ask_bid_ratio-1)*30))
    news=50; risk=75
    return {"quality":round(momentum*.3+volume_score*.25+depth*.25+news*.1+risk*.1,2),"momentum":momentum,"volume":volume_score,"depth":depth,"news":news,"risk":risk}
