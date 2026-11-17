from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database(database_url: str) -> None:
    """Apply runner migrations to one database URL.

    Kept as a small callable so the test suite can begin from the same migration
    path used by containers and CI rather than `metadata.create_all()`.
    """
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
