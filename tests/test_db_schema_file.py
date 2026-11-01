import re
from pathlib import Path

from app.models import Base

SCHEMA_FILE = Path(__file__).resolve().parents[1] / ".claude" / "rules" / "db-schema.sql"
_TABLE_BLOCK = re.compile(r"CREATE TABLE public\.(\w+) \((.*?)\n\);", re.DOTALL)


def _tables_in_schema_file() -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for name, body in _TABLE_BLOCK.findall(SCHEMA_FILE.read_text()):
        columns = [
            line.strip().split()[0]
            for line in body.strip().splitlines()
            if not line.strip().startswith("CONSTRAINT")
        ]
        tables[name] = columns
    return tables


def test_db_schema_file_tables_and_columns_match_models() -> None:
    from_models = {
        table.name: [column.name for column in table.columns]
        for table in Base.metadata.sorted_tables
    }

    assert _tables_in_schema_file() == from_models
