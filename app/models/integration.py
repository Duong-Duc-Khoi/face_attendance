"""
Integration settings for external AI providers.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.models.base import Base


class AIProviderSetting(Base):
    __tablename__ = "ai_provider_settings"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(30), unique=True, index=True, nullable=False)
    api_key_encrypted = Column(Text, default="")
    model = Column(String(100), default="")
    is_enabled = Column(Boolean, default=False)
    updated_by = Column(String(150), default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
