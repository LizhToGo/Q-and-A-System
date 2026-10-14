from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


@event.listens_for(Base.metadata, "after_create")
def _install_retrieval_tracking(metadata, connection, **_kwargs) -> None:
    if connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO retrieval_revision (id, generation, revision) "
        "VALUES (1, lower(hex(randomblob(16))), 0)"
    )
    for table in ("materials", "material_chunks"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS retrieval_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table} BEGIN "
                "UPDATE retrieval_revision SET revision = revision + 1 WHERE id = 1; END"
            )


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def create_tables() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to existing SQLite tables.
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("conversations")}
        if "title_source" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN title_source VARCHAR(20) NOT NULL DEFAULT 'pending'"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
