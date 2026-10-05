"""Unified fleet data model (SQLAlchemy)."""
from .schema import Base, create_all, engine

__all__ = ["Base", "create_all", "engine"]
