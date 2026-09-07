"""CLI end-to-end tests (src/oilfield/cli)."""

import pytest

from oilfield.cli import main


def _run(data_path, *argv):
    return main(["--data", str(data_path), *argv])


def test_cli_full_day(tmp_path, capsys):
    data = tmp_path / "state.json"
    assert _run(data, "init") == 0
    assert _run(data, "site", "add", "WH-01", "Склад") == 0
    assert _run(data, "loc", "add", "WH-01:яч-1", "--site", "WH-01") == 0
    assert _run(data, "item", "add", "tube-01", "--sku", "HKT-73",
                "--serial", "SN-1", "--cert", "c1") == 0
    assert _run(data, "item", "add", "tube-02", "--sku", "HKT-73",
                "--serial", "SN-2", "--cert", "c1") == 0
    assert _run(data, "cert", "add", "c1", "--sku", "HKT-73") == 0
    assert _run(data, "keeper", "add", "Иван") == 0
    assert _run(data, "receive", "tube-01", "WH-01:яч-1", "--keeper", "Иван") == 0
    assert _run(data, "receive", "tube-02", "WH-01:яч-1", "--keeper", "Иван") == 0

    # snapshot sees only one of two → report must flag пересортица.
    assert _run(data, "snapshot", "WH-01:яч-1", "--item", "tube-01", "--keeper", "Иван") == 0
    _run(data, "report", "WH-01:яч-1")
    out = capsys.readouterr().out
    assert "пересортица" in out
    assert "tube-02" in out

    # search by serial works
    _run(data, "find", "SN-1")
    out = capsys.readouterr().out
    assert "tube-01" in out
