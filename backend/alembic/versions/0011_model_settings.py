"""DeepSeek 不可变模型配置和企业内当前版本指针。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0011_model_settings"
down_revision = "0010_import_retention"
branch_labels = None
depends_on = None


def upgrade():
    identity = mysql.BIGINT(unsigned=True)
    op.create_table(
        "model_configs",
        sa.Column("id", identity, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", identity, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "enterprise_id",
            identity,
            sa.ForeignKey("enterprises.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(2048), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("secret_cipher", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("nonce", mysql.BINARY(12), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.UniqueConstraint("enterprise_id", "config_version", name="uq_model_config_version"),
        sa.UniqueConstraint("id", "enterprise_id", name="uq_model_config_enterprise"),
        sa.CheckConstraint("daily_limit > 0", name="ck_model_daily_limit"),
        sa.CheckConstraint("timeout_seconds BETWEEN 30 AND 300", name="ck_model_timeout"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.add_column("enterprises", sa.Column("active_model_config_id", identity, nullable=True))
    op.create_foreign_key(
        "fk_enterprise_active_model",
        "enterprises",
        "model_configs",
        ["active_model_config_id", "id"],
        ["id", "enterprise_id"],
        ondelete="RESTRICT",
    )


def downgrade():
    op.drop_constraint("fk_enterprise_active_model", "enterprises", type_="foreignkey")
    op.drop_column("enterprises", "active_model_config_id")
    op.drop_table("model_configs")
