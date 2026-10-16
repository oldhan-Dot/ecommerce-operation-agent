"""人工竞品资料：保留来源、采集日期与同店本品关联。

迁移版本： 0004_competitors
前置版本： 0003_products
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0004_competitors"
down_revision = "0003_products"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 独立增量迁移，不修改既有商品或身份记录。
    op.create_table(
        "competitors",
        sa.Column(
            "store_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=64, collation="utf8mb4_bin"), nullable=False),
        sa.Column(
            "product_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=True),
        sa.Column("spec", sa.String(length=200), nullable=True),
        sa.Column("price", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("selling_points", sa.Text(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("collected_at", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["product_id", "store_id"],
            ["products.id", "products.store_id"],
            name="fk_competitor_product_store",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "code", name="uq_competitor_code"),
    )
    op.create_index(
        "ix_competitor_scope", "competitors", ["store_id", "status", "collected_at"], unique=False
    )


def downgrade() -> None:
    # 只供人工确认后的回退使用，日常启动不执行。
    op.drop_index("ix_competitor_scope", table_name="competitors")
    op.drop_table("competitors")
