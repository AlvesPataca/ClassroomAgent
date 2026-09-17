from pathlib import Path
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event, inspect

from app.persistence.models import Base


def open_database(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(path)), connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def configure(connection: Any, record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")

    # Idempotent for this first schema. Refuse unknown future versions.
    with engine.begin() as connection:
        version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
        if version not in (0, 1, 2, 3):
            raise ValueError("Unsupported database schema version")
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            actual = (
                {c["name"] for c in inspector.get_columns(table.name)}
                if table.name in existing
                else set()
            )
            if table.name in existing and actual != set(table.columns.keys()):
                raise ValueError("Incompatible database schema; migration required")
        Base.metadata.create_all(connection)
        connection.exec_driver_sql("PRAGMA user_version=3")
    return engine
