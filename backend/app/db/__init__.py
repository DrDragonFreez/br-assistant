"""Слой доступа к данным: PostgreSQL, Redis, Qdrant."""

from app.db.base import Base
from app.db.postgres import get_session, lifespan_postgres, session_scope
from app.db.qdrant_client import get_qdrant, lifespan_qdrant
from app.db.redis import get_redis, lifespan_redis

__all__ = [
    "Base",
    "get_session",
    "session_scope",
    "lifespan_postgres",
    "get_redis",
    "lifespan_redis",
    "get_qdrant",
    "lifespan_qdrant",
]
