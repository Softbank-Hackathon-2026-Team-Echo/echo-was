from pathlib import Path

from tests.db_naming import checkout_database_name


def test_checkout_database_name_differs_by_folder_and_keeps_test_suffix() -> None:
    first = checkout_database_name("echo_test", Path("/work/echo-was"))
    second = checkout_database_name("echo_test", Path("/work/echo-was-fix84"))

    assert first != second
    assert first.startswith("echo_") and first.endswith("_test")
    assert first == checkout_database_name("echo_test", Path("/work/echo-was"))
    assert len(first) <= 63
