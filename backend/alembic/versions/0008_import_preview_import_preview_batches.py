"""持久 CSV 预检：批次、规范行、错误与版本依赖。

迁移版本： 0008_import_preview
前置版本： 0007_ad_daily
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0008_import_preview"
down_revision = "0007_ad_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 新增预检暂存表及合法业务键生成列，不修改任何业务事实表。
    op.create_table(
        "import_batches",
        sa.Column(
            "enterprise_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "store_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=True,
        ),
        sa.Column("import_type", sa.String(length=32), nullable=False),
        sa.Column("file_sha256", sa.BINARY(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("overwrite_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("preview_token", sa.String(length=64), nullable=True),
        sa.Column("preview_expires_at", sa.DateTime(), nullable=True),
        sa.Column("overwrite_confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirm_key", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=256), nullable=True),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_by",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["enterprise_id"], ["enterprises.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creator_id", "confirm_key", name="uq_import_confirm"),
    )
    op.create_index(
        "ix_import_queue", "import_batches", ["status", "created_at", "id"], unique=False
    )
    op.create_table(
        "import_dependencies",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "batch_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column(
            "entity_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "entity_type", "entity_id", name="uq_import_dependency"),
    )
    op.create_table(
        "import_rows",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "batch_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("business_key", sa.String(length=256, collation="utf8mb4_bin"), nullable=False),
        sa.Column("normalized_payload", sa.JSON(), nullable=False),
        sa.Column("before_payload", sa.JSON(), nullable=True),
        sa.Column("expected_target_version", sa.Integer(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "valid_business_key",
            sa.String(length=256, collation="utf8mb4_bin"),
            sa.Computed("CASE WHEN error_count=0 THEN business_key ELSE NULL END", persisted=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "row_no", name="uq_import_row"),
        sa.UniqueConstraint("batch_id", "valid_business_key", name="uq_import_valid_key"),
    )


def downgrade() -> None:
    # 人工确认回退时按依赖顺序移除；日常启动不执行。
    op.drop_table("import_rows")
    op.drop_table("import_dependencies")
    op.drop_index("ix_import_queue", table_name="import_batches")
    op.drop_table("import_batches")
