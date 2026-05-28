"""
app/models/branch.py
Model chi nhanh / dia diem nha hang.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String

from app.models.base import Base


class Branch(Base):
    __tablename__ = "branches"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(150), nullable=False, unique=True, index=True)
    address    = Column(String(255), default="")
    phone      = Column(String(30), default="")
    latitude   = Column(Float, nullable=True)
    longitude  = Column(Float, nullable=True)
    geofence_radius_m = Column(Integer, default=50)
    mobile_attendance_enabled = Column(Boolean, default=False, index=True)
    is_active  = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
