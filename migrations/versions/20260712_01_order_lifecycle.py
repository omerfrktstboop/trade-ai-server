"""authoritative order lifecycle and operational constraints"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260712_01"
down_revision = None
branch_labels = None
depends_on = None

_NEW_COLUMNS = {
    "request_fingerprint": sa.Column("request_fingerprint", sa.String(64)),
    "order_qty": sa.Column("order_qty", sa.Float(), server_default="0", nullable=False),
    "limit_price": sa.Column("limit_price", sa.Float()),
    "state": sa.Column("state", sa.String(32), server_default="RESERVED", nullable=False),
    "order_type": sa.Column("order_type", sa.String(16), server_default="LIMIT", nullable=False),
    "rounded_limit_price": sa.Column("rounded_limit_price", sa.Float()),
    "filled_qty": sa.Column("filled_qty", sa.Float(), server_default="0", nullable=False),
    "last_fill_qty": sa.Column("last_fill_qty", sa.Float(), server_default="0", nullable=False),
    "avg_price": sa.Column("avg_price", sa.Float()),
    "reservation_created_at": sa.Column("reservation_created_at", sa.DateTime(timezone=True)),
    "send_started_at": sa.Column("send_started_at", sa.DateTime(timezone=True)),
    "sent_at": sa.Column("sent_at", sa.DateTime(timezone=True)),
    "finalized_at": sa.Column("finalized_at", sa.DateTime(timezone=True)),
    "error_code": sa.Column("error_code", sa.String(64)),
    "error_message": sa.Column("error_message", sa.Text()),
    "config_version": sa.Column("config_version", sa.String(64)),
    "config_hash": sa.Column("config_hash", sa.String(64)),
    "profile_code": sa.Column("profile_code", sa.String(64)),
    "decision_id": sa.Column("decision_id", sa.String(64)),
    "updated_at": sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
}
_RANK = {"FILLED": 100, "REJECTED": 100, "CANCELED": 100, "CANCELLED": 100, "EXPIRED": 100, "PARTIALLY_FILLED": 40, "CANCEL_REQUESTED": 30, "NEW": 20, "SEND_UNKNOWN": 15, "SENT_PENDING": 10, "SEND_IN_PROGRESS": 5, "RESERVED": 1}


def _create_order_logs() -> None:
    op.create_table("order_logs",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False), sa.Column("action", sa.String(8), nullable=False),
        sa.Column("qty", sa.Float(), server_default="0"), sa.Column("price", sa.Float()), sa.Column("order_id", sa.String(64)),
        sa.Column("status", sa.String(32), server_default="RESERVED"), sa.Column("mode", sa.String(16), server_default="PAPER"),
        sa.Column("matrix_message", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        *[column.copy() for column in _NEW_COLUMNS.values()], sa.UniqueConstraint("request_id", name="uq_order_logs_request_id"))


def _merge_duplicates(bind, columns: set[str]) -> None:
    duplicates = bind.execute(sa.text("SELECT request_id FROM order_logs GROUP BY request_id HAVING COUNT(*) > 1")).scalars().all()
    for request_id in duplicates:
        rows = list(bind.execute(sa.text("SELECT * FROM order_logs WHERE request_id=:rid ORDER BY id"), {"rid": request_id}).mappings())
        winner = max(rows, key=lambda row: (_RANK.get(str(row.get("status") or "").upper(), 0), float(row.get("qty") or 0), row["id"]))
        messages = " | ".join(dict.fromkeys(str(row.get("matrix_message")) for row in rows if row.get("matrix_message")))
        updates = {"status": winner.get("status"), "qty": max(float(row.get("qty") or 0) for row in rows), "matrix_message": messages or winner.get("matrix_message")}
        if "order_id" in columns:
            updates["order_id"] = next((row.get("order_id") for row in reversed(rows) if row.get("order_id")), None)
        assignments = ", ".join(f"{key}=:{key}" for key in updates)
        bind.execute(sa.text(f"UPDATE order_logs SET {assignments} WHERE id=:id"), {**updates, "id": winner["id"]})
        bind.execute(sa.text("DELETE FROM order_logs WHERE request_id=:rid AND id<>:id"), {"rid": request_id, "id": winner["id"]})


def upgrade() -> None:
    bind = op.get_bind(); inspector = sa.inspect(bind); tables = set(inspector.get_table_names())
    if "order_logs" not in tables:
        _create_order_logs()
        with op.batch_alter_table("order_logs") as batch:
            batch.alter_column("updated_at", existing_type=sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"))
    else:
        columns = {column["name"] for column in inspector.get_columns("order_logs")}
        _merge_duplicates(bind, columns)
        for name, column in _NEW_COLUMNS.items():
            if name not in columns:
                op.add_column("order_logs", column)
        if "updated_at" not in columns:
            bind.execute(sa.text("UPDATE order_logs SET updated_at=COALESCE(created_at, CURRENT_TIMESTAMP)"))
            with op.batch_alter_table("order_logs") as batch:
                batch.alter_column("updated_at", existing_type=sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"))
        bind.execute(sa.text("""
            UPDATE order_logs
            SET order_qty=CASE WHEN order_qty IS NULL OR order_qty=0 THEN COALESCE(qty, 0) ELSE order_qty END,
                limit_price=COALESCE(limit_price, price),
                state=CASE WHEN state IS NULL OR state='' OR state='RESERVED' THEN COALESCE(status, 'RESERVED') ELSE state END,
                error_message=COALESCE(error_message, matrix_message)
        """))
        uniques = {item.get("name") for item in inspector.get_unique_constraints("order_logs")}
        if "uq_order_logs_request_id" not in uniques:
            with op.batch_alter_table("order_logs") as batch:
                batch.create_unique_constraint("uq_order_logs_request_id", ["request_id"])
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("order_logs")}
    if "ix_order_logs_state_updated" not in indexes:
        op.create_index("ix_order_logs_state_updated", "order_logs", ["status", "updated_at"])
    if "ix_order_logs_order_id_not_null" not in indexes and bind.dialect.name == "postgresql":
        op.create_index("ix_order_logs_order_id_not_null", "order_logs", ["order_id"], unique=False, postgresql_where=sa.text("order_id IS NOT NULL"))
    if "manual_approval_requests" in set(inspector.get_table_names()):
        bind.execute(sa.text("UPDATE manual_approval_requests SET status='PENDING_APPROVAL' WHERE status='PENDING'"))


def downgrade() -> None:
    bind = op.get_bind(); inspector = sa.inspect(bind)
    if "manual_approval_requests" in set(inspector.get_table_names()):
        bind.execute(sa.text("UPDATE manual_approval_requests SET status='PENDING' WHERE status='PENDING_APPROVAL'"))
    indexes = {item["name"] for item in inspector.get_indexes("order_logs")}
    for name in ("ix_order_logs_order_id_not_null", "ix_order_logs_state_updated"):
        if name in indexes: op.drop_index(name, table_name="order_logs")
    with op.batch_alter_table("order_logs") as batch:
        uniques = {item.get("name") for item in inspector.get_unique_constraints("order_logs")}
        if "uq_order_logs_request_id" in uniques: batch.drop_constraint("uq_order_logs_request_id", type_="unique")
        columns = {column["name"] for column in inspector.get_columns("order_logs")}
        for name in reversed(tuple(_NEW_COLUMNS)):
            if name in columns: batch.drop_column(name)
