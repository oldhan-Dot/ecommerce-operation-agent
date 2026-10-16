"""商品日经营：业务键唯一，组合外键限定同店关联。

迁移版本： 0006_product_daily
前置版本： 0005_store_daily
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0006_product_daily"
down_revision = "0005_store_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 只新增商品日数据表，不修改店铺汇总，也不自动生成缺失日记录。
    op.create_table(
        "product_daily",
        sa.Column(
            "store_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
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
        sa.CheckConstraint(
            "paid_amount >= 0 AND refund_amount >= 0", name="ck_product_daily_money"
        ),
        sa.CheckConstraint("paid_orders >= 0 AND visitors >= 0", name="ck_product_daily_counts"),
        sa.ForeignKeyConstraint(
            ["product_id", "store_id"],
            ["products.id", "products.store_id"],
            name="fk_product_daily_store",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "business_date", name="uq_product_daily_date"),
    )
    op.create_index(
        "ix_product_daily_scope",
        "product_daily",
        ["store_id", "business_date", "product_id"],
        unique=False,
    )


def downgrade() -> None:
    # 仅用于人工确认后的回退，日常启动不执行。
    op.drop_index("ix_product_daily_scope", table_name="product_daily")
    op.drop_table("product_daily")
