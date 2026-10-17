"""商品资料分析任务、不可变输入快照与首版草稿。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0013_ai_tasks"
down_revision = "0012_model_calls"
branch_labels = None
depends_on = None


def record_columns():
    """为本迁移固定通用记录字段，避免引用运行期ORM导致历史迁移漂移。"""
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
    ]


def upgrade():
    identity, precise = mysql.BIGINT(unsigned=True), mysql.DATETIME(fsp=6)
    op.create_table(
        "ai_tasks",
        *record_columns(),
        sa.Column("enterprise_id", identity, sa.ForeignKey("enterprises.id"), nullable=False),
        sa.Column("store_id", identity, sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("creator_id", identity, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("request_hash", mysql.BINARY(32), nullable=False),
        sa.Column("parent_task_id", identity, sa.ForeignKey("ai_tasks.id"), nullable=True),
        sa.Column("root_task_id", identity, sa.ForeignKey("ai_tasks.id"), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("model_config_id", identity, sa.ForeignKey("model_configs.id"), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_until", precise, nullable=True),
        sa.Column("queued_at", precise, nullable=False),
        sa.Column("started_at", precise, nullable=True),
        sa.Column("deadline_at", precise, nullable=True),
        sa.Column("finished_at", precise, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.UniqueConstraint("creator_id", "idempotency_key", name="uq_ai_task_request"),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name="ck_ai_task_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_ai_task_queue", "ai_tasks", ["status", "queued_at", "id"])
    op.create_index("ix_ai_task_scope", "ai_tasks", ["store_id", "creator_id", "created_at", "id"])
    op.create_table(
        "input_snapshots",
        sa.Column("id", identity, primary_key=True, autoincrement=True),
        sa.Column("task_id", identity, sa.ForeignKey("ai_tasks.id"), nullable=False, unique=True),
        *[
            sa.Column(name, sa.JSON(), nullable=False)
            for name in ("input_params", "source_refs", "data_copy", "metrics", "missing_items")
        ],
        *[
            sa.Column(name, sa.String(32), nullable=False)
            for name in ("schema_version", "metric_version", "prompt_version")
        ],
        sa.Column("sha256", mysql.BINARY(32), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "artifacts",
        *record_columns(),
        sa.Column("task_id", identity, sa.ForeignKey("ai_tasks.id"), nullable=False, unique=True),
        sa.Column("enterprise_id", identity, sa.ForeignKey("enterprises.id"), nullable=False),
        sa.Column("store_id", identity, sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("creator_id", identity, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "artifact_versions",
        *record_columns(),
        sa.Column("artifact_id", identity, sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.UniqueConstraint("artifact_id", "version_no", name="uq_artifact_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.add_column("model_calls", sa.Column("task_id", identity, nullable=True))
    op.create_foreign_key("fk_model_call_task", "model_calls", "ai_tasks", ["task_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_model_call_task", "model_calls", type_="foreignkey")
    op.drop_column("model_calls", "task_id")
    for table in ("artifact_versions", "artifacts", "input_snapshots", "ai_tasks"):
        op.drop_table(table)
