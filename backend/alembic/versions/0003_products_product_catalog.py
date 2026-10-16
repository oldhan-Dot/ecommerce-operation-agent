"""商品与 SKU：组合外键限制同店归属，保留已有身份与店铺表。

迁移版本： 0003_products
前置版本： 0002_identity
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0003_products"
down_revision = "0002_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 只增加两张资料表，不修改已有业务记录，也不初始化示例商品。
    op.create_table(
        "products",
        sa.Column(
            "store_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("selling_points", sa.Text(), nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=True),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
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
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "store_id", name="uq_product_store"),
        sa.UniqueConstraint("store_id", "code", name="uq_product_code"),
    )
    op.create_index("ix_product_scope", "products", ["store_id", "status", "id"], unique=False)
    op.create_table(
        "skus",
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
        sa.Column("code", sa.String(length=64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("spec_name", sa.String(length=200), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("cost", sa.Numeric(precision=18, scale=2), nullable=True),
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
            name="fk_sku_product_store",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "code", name="uq_sku_code"),
    )


def downgrade() -> None:
    # 人工回退时先移除依赖表；日常部署与重启不执行此函数。
    op.drop_table("skus")
    op.drop_index("ix_product_scope", table_name="products")
    op.drop_table("products")
