"""店铺日经营：业务键唯一与数据库非负约束。

迁移版本： 0005_store_daily
前置版本： 0004_competitors
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0005_store_daily"
down_revision = "0004_competitors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 独立增量迁移，仅新增店铺日汇总，不生成缺失日期或初始经营数据。
    op.create_table(
        "store_daily",
        sa.Column(
            "store_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("paid_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("paid_orders", sa.BigInteger(), nullable=False),
        sa.Column("visitors", sa.BigInteger(), nullable=False),
        sa.Column("refund_amount", sa.Numeric(precision=18, scale=2), nullable=False),
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
        sa.CheckConstraint("paid_amount >= 0 AND refund_amount >= 0", name="ck_store_daily_money"),
        sa.CheckConstraint("paid_orders >= 0 AND visitors >= 0", name="ck_store_daily_counts"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "business_date", name="uq_store_daily_date"),
    )


def downgrade() -> None:
    # 仅供人工确认的回退使用；正常启动或重启不得调用。
    op.drop_table("store_daily")
