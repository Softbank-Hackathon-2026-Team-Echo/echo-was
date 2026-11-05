from pathlib import Path

from tests.db_naming import checkout_database_name


def test_checkout_database_name_differs_by_folder_and_keeps_test_suffix() -> None:
    first = checkout_database_name("echo_test", Path("/work/echo-was"))
    second = checkout_database_name("echo_test", Path("/work/echo-was-fix84"))

    assert first != second
    assert first.startswith("echo_") and first.endswith("_test")
    assert first == checkout_database_name("echo_test", Path("/work/echo-was"))
    assert len(first) <= 63


def test_checkout_database_name_differs_by_worker_and_keeps_test_suffix() -> None:
    root = Path("/work/echo-was")
    digest = checkout_database_name("echo_test", root).removeprefix("echo_").removesuffix("_test")

    first = checkout_database_name("echo_test", root, worker_id="gw0")
    second = checkout_database_name("echo_test", root, worker_id="gw7")

    assert first == f"echo_{digest}_gw0_test"
    assert second == f"echo_{digest}_gw7_test"
    assert first.endswith("_test") and second.endswith("_test")
    assert len(first) <= 63 and len(second) <= 63


def test_checkout_database_name_without_worker_keeps_serial_name() -> None:
    root = Path("/work/echo-was")

    assert checkout_database_name("echo_test", root, worker_id=None) == checkout_database_name(
        "echo_test", root
    )
