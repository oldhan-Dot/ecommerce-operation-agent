"""持久Worker心跳，API与Worker独立进程共享健康状态。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0015_observability"
down_revision = "0014_artifact_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_heartbeats",
        sa.Column("instance_id", sa.String(64), primary_key=True),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("seen_at", mysql.DATETIME(fsp=6), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_service_heartbeats_seen_at", "service_heartbeats", ["seen_at"])


def downgrade():
    op.drop_table("service_heartbeats")
