"""日报记录保留最近一次 CSV 写入批次，手工创建的记录允许为空。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0009_import_confirm"
down_revision = "0008_import_preview"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("store_daily", "product_daily", "ad_daily"):
        op.add_column(table, sa.Column("batch_id", mysql.BIGINT(unsigned=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_batch", table, "import_batches", ["batch_id"], ["id"], ondelete="RESTRICT"
        )


def downgrade():
    for table in ("ad_daily", "product_daily", "store_daily"):
        op.drop_constraint(f"fk_{table}_batch", table, type_="foreignkey")
        op.drop_column(table, "batch_id")
