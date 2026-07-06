"""Tests for the order-result endpoint and OrderLog model."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.db.order_log import OrderLog
from app.routers.order_result import OrderResultRequest


# ── Model tests ───────────────────────────────────────────────────────────────


class TestOrderLogModel:
    """OrderLog SQLAlchemy model field coverage."""

    def test_matrix_message_is_nullable_text(self):
        """matrix_message defaults to None and can store long strings."""
        entry = OrderLog(
            request_id="req-1",
            symbol="THYAO",
            action="BUY",
            qty=100.0,
            price=71.25,
            status="FILLED",
        )
        assert entry.matrix_message is None

        long_msg = "Exchange error: insufficient balance — order rejected"
        entry.matrix_message = long_msg
        assert entry.matrix_message == long_msg

    def test_matrix_message_stored_when_provided(self):
        entry = OrderLog(
            request_id="req-2",
            symbol="AKBNK",
            action="SELL",
            qty=50.0,
            price=42.30,
            status="CANCELED",
            matrix_message="No liquidity at target price",
        )
        assert entry.matrix_message == "No liquidity at target price"

    def test_order_id_nullable(self):
        """orderId may be None when exchange didn't assign one."""
        entry = OrderLog(
            request_id="req-3",
            symbol="THYAO",
            action="BUY",
            qty=10.0,
            price=100.0,
            status="REJECTED",
            matrix_message="Order rejected by risk check",
        )
        assert entry.order_id is None


# ── Request schema tests ──────────────────────────────────────────────────────


class TestOrderResultRequest:
    """Pydantic schema validation for the order-result payload."""

    def test_parses_camel_case_matrix_message(self):
        body = OrderResultRequest.model_validate(
            {
                "requestId": "r-1",
                "symbol": "THYAO",
                "action": "BUY",
                "qty": 100,
                "price": 71.25,
                "status": "FILLED",
                "matriksMessage": "OK — executed at 71.25",
                "orderId": "XCH-99",
            }
        )
        assert body.request_id == "r-1"
        # Access via Python attribute name (not the alias)
        body_dict = body.model_dump()
        assert body_dict["matriks_message"] == "OK — executed at 71.25"
        assert body_dict["order_id"] == "XCH-99"

    def test_matrix_message_required(self):
        with pytest.raises(ValueError, match="matriksMessage"):
            OrderResultRequest.model_validate(
                {
                    "requestId": "r-2",
                    "symbol": "THYAO",
                    "action": "BUY",
                    "qty": 100,
                    "price": 71.25,
                    "status": "FILLED",
                }
            )

    def test_order_id_optional(self):
        body = OrderResultRequest.model_validate(
            {
                "requestId": "r-3",
                "symbol": "THYAO",
                "action": "BUY",
                "qty": 100,
                "price": 71.25,
                "status": "FILLED",
                "matriksMessage": "OK",
            }
        )
        body_dict = body.model_dump()
        assert body_dict.get("order_id") is None

    @pytest.mark.asyncio
    async def test_returns_ok_response_even_on_db_error(self):
        """When DB commit fails, endpoint still returns {'status': 'ok'}."""
        from app.routers.order_result import record_order_result

        request = OrderResultRequest.model_validate(
            {
                "requestId": "r-4",
                "symbol": "THYAO",
                "action": "BUY",
                "qty": 100,
                "price": 71.25,
                "status": "FILLED",
                "matriksMessage": "fine",
            }
        )

        with patch(
            "app.routers.order_result.async_session_factory",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            resp = await record_order_result(request)
            assert resp.status == "ok"

    def test_matrix_message_passed_to_order_log(self):
        """Verify matrix_message flows from request body into OrderLog."""
        body = OrderResultRequest.model_validate(
            {
                "requestId": "r-5",
                "symbol": "THYAO",
                "action": "SELL",
                "qty": 50,
                "price": 80.0,
                "status": "FILLED",
                "matriksMessage": "Partial fill: 50/100",
                "orderId": "XCH-200",
            }
        )

        body_dict = body.model_dump()
        entry = OrderLog(
            request_id=body_dict["request_id"],
            symbol=body_dict["symbol"],
            action=body_dict["action"],
            qty=body_dict["qty"],
            price=body_dict["price"],
            status=body_dict["status"],
            order_id=body_dict["order_id"],
            matrix_message=body_dict["matriks_message"],
        )
        assert entry.matrix_message == "Partial fill: 50/100"
        assert entry.order_id == "XCH-200"
        assert entry.request_id == "r-5"
