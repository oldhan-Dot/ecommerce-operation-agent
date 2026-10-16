"""投放日经营：渠道业务键、非负约束与归因口径校验。

迁移版本： 0007_ad_daily
前置版本： 0006_product_daily
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0007_ad_daily"
down_revision = "0006_product_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 独立新增人工投放日数据，不合并渠道归因，也不生成示例投放事实。
    op.create_table(
        "ad_daily",
        sa.Column(
            "store_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("spend", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=False),
        sa.Column("clicks", sa.BigInteger(), nullable=False),
        sa.Column("attributed_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("attribution_note", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint(
            "attributed_amount IS NULL OR (attribution_note IS NOT NULL "
            "AND CHAR_LENGTH(TRIM(attribution_note)) > 0)",
            name="ck_ad_daily_attribution",
        ),
        sa.CheckConstraint("impressions >= 0 AND clicks >= 0", name="ck_ad_daily_counts"),
        sa.CheckConstraint(
            "spend >= 0 AND (attributed_amount IS NULL OR attributed_amount >= 0)",
            name="ck_ad_daily_money",
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "channel", "business_date", name="uq_ad_daily_date"),
    )
    op.create_index("ix_ad_daily_scope", "ad_daily", ["store_id", "business_date"], unique=False)


def downgrade() -> None:
    # 仅供人工确认后的回退，日常部署或重启不执行。
    op.drop_index("ix_ad_daily_scope", table_name="ad_daily")
    op.drop_table("ad_daily")
