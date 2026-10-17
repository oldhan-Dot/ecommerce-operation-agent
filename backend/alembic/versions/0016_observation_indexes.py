"""按企业和完成时间限定观测扫描范围。"""

from alembic import op

revision = "0016_observation_indexes"
down_revision = "0015_observability"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_model_call_observe", "model_calls", ["enterprise_id", "finished_at", "status"]
    )
    op.create_index("ix_import_observe", "import_batches", ["finished_at", "enterprise_id"])


def downgrade():
    op.drop_index("ix_import_observe", "import_batches")
    op.drop_index("ix_model_call_observe", "model_calls")
