"""导入清理时间及幂等标记，原有终态按最后更新时间回填完成时间。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0010_import_retention"
down_revision = "0009_import_confirm"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("finished_at", "rows_purged_at", "file_deleted_at", "cleanup_retry_at"):
        op.add_column("import_batches", sa.Column(name, mysql.DATETIME(fsp=6), nullable=True))
    op.execute(
        "UPDATE import_batches SET finished_at = updated_at "
        "WHERE status IN ('SUCCEEDED', 'FAILED', 'INVALID')"
    )
    op.create_index("ix_import_retention", "import_batches", ["status", "finished_at", "id"])


def downgrade():
    op.drop_index("ix_import_retention", table_name="import_batches")
    for name in ("cleanup_retry_at", "file_deleted_at", "rows_purged_at", "finished_at"):
        op.drop_column("import_batches", name)
