"""Tests for the claim-version fence (code-identity stamp on cron claims).

A fire/run claim carries the checkout SHA that minted it. A gateway running
different code must refuse to refresh or deliver that claim: otherwise a stale
process can silently re-own a run it no longer owns after an update.

These exercise the real store against a temp HERMES_HOME (no mocks) per the
E2E-over-mocks discipline for file-touching code.
"""
from datetime import UTC, datetime

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json doesn't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def test_fire_claim_stamps_code_identity(temp_home):
    """A minted fire claim carries the current code identity, not a placeholder."""
    from cron import jobs

    job = jobs.create_job(prompt="x", schedule="every 5m", name="stamp")
    assert jobs.claim_job_for_fire(job["id"]) is True

    claim = jobs.get_job(job["id"])["fire_claim"]
    assert claim["version"] == jobs._claim_version()
    assert claim["version"]  # never empty


def test_claim_is_live_refuses_other_code_version(temp_home):
    """A fresh claim minted by different code is not live for this process."""
    from cron import jobs

    claim = {"at": datetime.now(UTC).isoformat(), "by": "host:1", "version": "deadbeef"}
    assert jobs._claim_is_live(claim, datetime.now(UTC), 600) is False


def test_claim_is_live_accepts_legacy_claim_without_version(temp_home):
    """Pre-fence claims (no version field) stay honoured — backward compatible."""
    from cron import jobs

    claim = {"at": datetime.now(UTC).isoformat(), "by": "host:1"}
    assert jobs._claim_is_live(claim, datetime.now(UTC), 600) is True


def test_heartbeat_refuses_claim_from_other_code_version(temp_home, monkeypatch):
    """A process whose code changed under it cannot keep refreshing the claim."""
    from cron import jobs

    job = jobs.create_job(prompt="x", schedule="every 5m", name="hb")
    assert jobs.claim_job_for_fire(job["id"]) is True
    owner = jobs.get_job(job["id"])["fire_claim"]["by"]

    monkeypatch.setattr(jobs, "_claim_version", lambda: "other-sha")
    assert jobs.heartbeat_fire_claim(job["id"], expected_owner=owner) is False


def test_fire_claim_fence_refuses_other_code_version(temp_home, monkeypatch):
    """The delivery fence refuses an owner whose code identity no longer matches."""
    from cron import jobs

    job = jobs.create_job(prompt="x", schedule="every 5m", name="fence")
    assert jobs.claim_job_for_fire(job["id"]) is True
    owner = jobs.get_job(job["id"])["fire_claim"]["by"]

    with jobs.fire_claim_fence(job["id"], expected_owner=owner) as owns:
        assert owns is True

    monkeypatch.setattr(jobs, "_claim_version", lambda: "other-sha")
    with jobs.fire_claim_fence(job["id"], expected_owner=owner) as owns:
        assert owns is False
