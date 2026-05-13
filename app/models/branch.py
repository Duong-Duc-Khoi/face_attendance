"""
app/models/branch.py
Model chi nhanh / dia diem nha hang.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.models.base import Base


class Branch(Base):
    __tablename__ = "branches"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(150), nullable=False, unique=True, index=True)
    address    = Column(String(255), default="")
    phone      = Column(String(30), default="")
    is_active  = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
