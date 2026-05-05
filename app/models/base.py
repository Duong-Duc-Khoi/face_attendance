"""
app/models/base.py
Base declarative dùng chung cho tất cả models.
Import tại đây để tránh circular import.
"""

from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()
