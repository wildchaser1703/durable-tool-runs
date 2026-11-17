from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            pool_timeout=5,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=15000 -c lock_timeout=5000",
            },
        )
        self.sessions = sessionmaker(
            self.engine, expire_on_commit=False, class_=Session
        )

    def reset_schema(self) -> None:
        """Test-only teardown. Runtime schema changes go through Alembic."""
        from app import models  # noqa: F401 - registers the complete metadata

        Base.metadata.drop_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
