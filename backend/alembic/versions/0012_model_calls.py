"""持久模型调用、企业调度锁及上海自然日额度。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0012_model_calls"
down_revision = "0011_model_settings"
branch_labels = None
depends_on = None


def upgrade():
    identity = mysql.BIGINT(unsigned=True)
    precise = mysql.DATETIME(fsp=6)
    op.create_table(
        "scheduler_state",
        sa.Column("enterprise_id", identity, sa.ForeignKey("enterprises.id"), primary_key=True),
        sa.Column("updated_at", precise, nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "daily_call_usage",
        sa.Column("id", identity, primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", identity, sa.ForeignKey("enterprises.id"), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("consumed", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.UniqueConstraint("enterprise_id", "usage_date", name="uq_daily_call_usage"),
        sa.CheckConstraint("consumed >= 0 AND reserved >= 0", name="ck_call_usage_positive"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "model_calls",
        sa.Column("id", identity, primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", identity, sa.ForeignKey("enterprises.id"), nullable=False),
        sa.Column("config_id", identity, nullable=False),
        sa.Column("actor_id", identity, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("request_key", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("deadline_at", precise, nullable=False),
        sa.Column("created_at", precise, nullable=False),
        sa.Column("started_at", precise, nullable=True),
        sa.Column("finished_at", precise, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("usage_tokens", sa.JSON(), nullable=True),
        sa.UniqueConstraint("request_key", name="uq_model_call_key"),
        sa.ForeignKeyConstraint(
            ["config_id", "enterprise_id"],
            ["model_configs.id", "model_configs.enterprise_id"],
            name="fk_call_config_enterprise",
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED','STARTED','SUCCEEDED','FAILED','UNKNOWN','RELEASED')",
            name="ck_model_call_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_model_call_deadline", "model_calls", ["enterprise_id", "status", "deadline_at"]
    )
    op.create_index("ix_model_call_actor", "model_calls", ["actor_id", "status"])


def downgrade():
    op.drop_table("model_calls")
    op.drop_table("daily_call_usage")
    op.drop_table("scheduler_state")
