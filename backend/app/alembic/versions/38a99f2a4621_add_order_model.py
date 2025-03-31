"""add Order model

Revision ID: 38a99f2a4621
Revises: None
Create Date: <自动生成的日期>

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime, timezone
from enum import Enum

# revision identifiers, used by Alembic.
revision = '38a99f2a4621'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # 创建 order 表，使用 VARCHAR 而不是 ENUM
    op.create_table('order',
        sa.Column('id', UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', UUID(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='USD'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),  # 改用 String 类型
        sa.Column('payment_method', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('receipt_url', sa.String(), nullable=True),
        sa.Column('onchain_tx_url', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('order')
