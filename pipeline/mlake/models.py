"""Tiny SQL model runner: one model per file in pipeline/sql/, named after the table it creates."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any

import duckdb

from . import config


def run_model(
    con: duckdb.DuckDBPyConnection, name: str, sql_dir: Path | None = None, **params: str
) -> None:
    path = (sql_dir or config.SQL_DIR) / f"{name}.sql"
    sql = Template(path.read_text()).substitute(**params)
    con.execute(sql)


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> Any:
    """First column of the first row (aggregate queries always return one row)."""
    row = con.execute(sql, params).fetchone()
    assert row is not None
    return row[0]
