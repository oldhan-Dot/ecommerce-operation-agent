"""草稿版本审核状态元数据、幂等操作与追加审核记录。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0014_artifact_review"
down_revision = "0013_ai_tasks"
branch_labels = None
depends_on = None


def upgrade():
    identity, precise = mysql.BIGINT(unsigned=True), mysql.DATETIME(fsp=6)
    for name in ("submitted_at", "reviewed_at"):
        op.add_column("artifact_versions", sa.Column(name, precise, nullable=True))
    op.add_column("artifact_versions", sa.Column("reviewer_id", identity, nullable=True))
    op.add_column("artifact_versions", sa.Column("review_comment", sa.String(1000), nullable=True))
    op.create_foreign_key(
        "fk_version_reviewer", "artifact_versions", "users", ["reviewer_id"], ["id"]
    )
    op.create_table(
        "artifact_actions",
        sa.Column("id", identity, primary_key=True, autoincrement=True),
        sa.Column("version_id", identity, sa.ForeignKey("artifact_versions.id"), nullable=False),
        sa.Column("actor_id", identity, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column(
            "request_key", sa.String(64, collation="utf8mb4_bin"), nullable=False, unique=True
        ),
        sa.Column("request_hash", mysql.BINARY(32), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", precise, nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "artifact_reviews",
        sa.Column("id", identity, primary_key=True, autoincrement=True),
        sa.Column(
            "version_id",
            identity,
            sa.ForeignKey("artifact_versions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("reviewer_id", identity, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("comment", sa.String(1000), nullable=True),
        sa.Column("created_at", precise, nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade():
    op.drop_table("artifact_reviews")
    op.drop_table("artifact_actions")
    op.drop_constraint("fk_version_reviewer", "artifact_versions", type_="foreignkey")
    for name in ("review_comment", "reviewer_id", "reviewed_at", "submitted_at"):
        op.drop_column("artifact_versions", name)
