"""Policy rulebook tests (src/oilfield/policies)."""

from oilfield.policies import (
    certificate_required,
    fefo_issue,
    issue_authorized,
    location_known,
    sku_matches,
    temperature_in_range,
)


def test_sku_matches():
    ok, _ = sku_matches("HKT-73", "HKT-73")
    assert ok
    ok, reason = sku_matches("HKT-73", "HKT-89")
    assert not ok
    assert "sku_mismatch" in reason  # пересортица


def test_certificate_required():
    ok, _ = certificate_required("HKT-73", ["cert-1"])
    assert ok
    ok, reason = certificate_required("HKT-73", [])
    assert not ok
    assert "certificate_missing" in reason


def test_fefo_issue():
    expiry = {"lot-A": "2026-10-01", "lot-B": "2026-09-01"}
    ok, _ = fefo_issue(expiry, "lot-A")
    assert not ok  # lot-B expires earlier → cannot issue lot-A
    ok, _ = fefo_issue(expiry, "lot-B")
    assert ok


def test_temperature_range():
    ok, _ = temperature_in_range(12.0)
    assert ok
    ok, reason = temperature_in_range(-5.0)
    assert not ok
    assert "temperature_out_of_range" in reason


def test_issue_authorized():
    assert issue_authorized(True)[0]
    assert not issue_authorized(False)[0]


def test_location_known():
    assert location_known("PAD-12:cell-7", {"PAD-12:cell-7"})[0]
    assert not location_known("nowhere", {"PAD-12:cell-7"})[0]
