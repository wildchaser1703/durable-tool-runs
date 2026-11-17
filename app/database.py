from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url, future=True, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def create_schema(self) -> None:
        from app import models  # Ensure metadata includes every model.

        Base.metadata.create_all(self.engine)

    def reset_schema(self) -> None:
        from app import models

        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

