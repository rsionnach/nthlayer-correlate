# tests/test_correlate_command.py
"""Tests for the correlate CLI subcommand with mocked Prometheus."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_correlate.prometheus import (
    blast_radius_services,
    load_dependency_graph,
    verdict_to_event,
)
from nthlayer_correlate.types import EventType


# --- Fixtures ---

FRAUD_SPEC = """\
apiVersion: srm/v1
kind: ServiceReliabilityManifest
metadata:
  name: fraud-detect
  tier: critical
spec:
  type: ai-gate
  slos:
    availability:
      target: 99.9
      window: 30d
    reversal_rate:
      target: 0.05
      window: 7d
  dependencies:
    - name: feature-store
      type: database
      critical: true
"""

PAYMENT_SPEC = """\
apiVersion: srm/v1
kind: ServiceReliabilityManifest
metadata:
  name: payment-api
  tier: critical
spec:
  type: api
  slos:
    availability:
      target: 99.99
      window: 30d
  dependencies:
    - name: fraud-detect
      type: api
      critical: true
"""


@pytest.fixture
def specs_dir(tmp_path):
    (tmp_path / "fraud-detect.yaml").write_text(FRAUD_SPEC)
    (tmp_path / "payment-api.yaml").write_text(PAYMENT_SPEC)
    return tmp_path


# --- load_dependency_graph tests ---

def test_load_dependency_graph(specs_dir):
    graph = load_dependency_graph(str(specs_dir))
    assert "fraud-detect" in graph
    assert "payment-api" in graph
    assert "feature-store" in graph["fraud-detect"]["dependencies"]
    # Reverse dependency: payment-api depends on fraud-detect
    assert "payment-api" in graph["fraud-detect"]["dependents"]


def test_load_dependency_graph_empty(tmp_path):
    graph = load_dependency_graph(str(tmp_path))
    assert graph == {}


# --- blast_radius_services tests ---

def test_blast_radius_includes_trigger_and_dependents(specs_dir):
    graph = load_dependency_graph(str(specs_dir))
    affected = blast_radius_services("fraud-detect", graph)
    assert "fraud-detect" in affected
    assert "payment-api" in affected  # depends on fraud-detect
    assert "feature-store" in affected  # fraud-detect depends on it


def test_blast_radius_leaf_service(specs_dir):
    graph = load_dependency_graph(str(specs_dir))
    affected = blast_radius_services("payment-api", graph)
    assert "payment-api" in affected
    assert "fraud-detect" in affected  # payment-api depends on it


# --- verdict_to_event tests ---

def test_verdict_to_event():
    from nthlayer_learn import create

    v = create(
        subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
        judgment={"action": "flag", "confidence": 0.85},
        producer={"system": "nthlayer-measure"},
        metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
    )
    event = verdict_to_event(v)
    assert event.type == EventType.VERDICT
    assert event.service == "fraud-detect"
    assert event.payload["verdict_id"] == v.id
    assert event.payload["breach"] is True


# --- correlate_command integration test ---

def test_correlate_command_writes_correlation_verdict(specs_dir, tmp_path):
    """Full correlate command with mocked Prometheus returns correlation verdict."""
    from nthlayer_learn import SQLiteVerdictStore, create as verdict_create

    # Set up verdict store with a trigger evaluation verdict
    store_path = str(tmp_path / "verdicts.db")
    store = SQLiteVerdictStore(store_path)
    trigger = verdict_create(
        subject={"type": "evaluation", "ref": "fraud-detect", "summary": "reversal rate breach"},
        judgment={"action": "flag", "confidence": 0.9},
        producer={"system": "nthlayer-measure"},
        metadata={"custom": {
            "slo_type": "judgment",
            "slo_name": "reversal_rate",
            "breach": True,
            "consecutive": 3,
        }},
    )
    store.put(trigger)

    # Mock Prometheus to return some alerts and metric breaches
    async def mock_fetch_alerts(client, url, services):
        from nthlayer_correlate.types import SitRepEvent, EventType
        return [SitRepEvent(
            id="alert-1",
            timestamp="2026-03-25T10:00:00Z",
            source="prometheus",
            type=EventType.ALERT,
            service="fraud-detect",
            environment="production",
            severity=0.8,
            payload={"alert_name": "FraudDetectHighErrorRate"},
        )]

    async def mock_fetch_breaches(client, url, services, window_minutes=30):
        from nthlayer_correlate.types import SitRepEvent, EventType
        return [SitRepEvent(
            id="breach-1",
            timestamp="2026-03-25T10:01:00Z",
            source="prometheus",
            type=EventType.METRIC_BREACH,
            service="fraud-detect",
            environment="production",
            severity=0.7,
            payload={"metric": "slo:error_budget:ratio", "value": -0.05},
        )]

    from nthlayer_correlate.cli import correlate_command

    with patch("nthlayer_correlate.prometheus.fetch_alerts", side_effect=mock_fetch_alerts), \
         patch("nthlayer_correlate.prometheus.fetch_metric_breaches", side_effect=mock_fetch_breaches):
        # Need to also patch the import inside the function
        result = correlate_command(
            trigger_verdict_id=trigger.id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
        )

    assert result == 0

    # Check that a correlation verdict was written
    from nthlayer_learn import VerdictFilter
    corr_verdicts = store.query(VerdictFilter(subject_type="correlation", limit=10))
    assert len(corr_verdicts) >= 1
    cv = corr_verdicts[0]
    assert cv.subject.ref == "fraud-detect"
    assert cv.producer.system == "sitrep"
    assert trigger.id in cv.lineage.context


def test_correlate_command_missing_verdict(tmp_path):
    """Returns error code 1 when trigger verdict doesn't exist."""
    from nthlayer_learn import SQLiteVerdictStore

    store_path = str(tmp_path / "verdicts.db")
    SQLiteVerdictStore(store_path)  # create empty store

    from nthlayer_correlate.cli import correlate_command

    result = correlate_command(
        trigger_verdict_id="vrd-nonexistent",
        prometheus_url="http://mock:9090",
        specs_dir=str(tmp_path),
        verdict_store_path=store_path,
    )
    assert result == 1
