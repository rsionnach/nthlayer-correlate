# SitRep Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build SitRep end-to-end — event ingestion, indexed storage, pre-correlation engine, snapshot generation, model interface, verdict output, state machine, and CLI. Tier 1 (WebhookIngester + SQLite FTS5).

**Architecture:** Events arrive via HTTP webhook → stored in SQLite FTS5 → pre-correlation engine groups by time/topology/changes (all deterministic transport) → snapshot generator applies token budget and caching → model interprets correlations (judgment) → output as verdicts with lineage. State machine drives snapshot frequency.

**Tech Stack:** Python 3.11+, `verdict` library (path dep: `pip install -e ../../nthlayer-learn/lib/python`), `anthropic` SDK, `pyyaml`, `structlog`, raw asyncio TCP (no web framework), SQLite FTS5.

**Prerequisites:** Install verdict library before starting: `pip install -e ../../nthlayer-learn/lib/python` (or add to pyproject.toml as path dependency).

**Spec:** `sitrep/docs/superpowers/specs/2026-03-17-sitrep-phase2-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `scenarios/synthetic/*.yaml` (5 files) | Test fixtures — event streams with expected outcomes |
| `src/sitrep/__init__.py` | Package root |
| `src/sitrep/types.py` | `SitRepEvent`, `EventType`, `CorrelationGroup`, `TemporalGroup`, `ChangeCandidate`, `TopologyCorrelation`, `AgentState` |
| `src/sitrep/config.py` | `SitRepConfig` dataclass, YAML loading |
| `src/sitrep/store/protocol.py` | `EventStore` Protocol (9 methods) |
| `src/sitrep/store/sqlite.py` | SQLite FTS5 implementation of EventStore |
| `src/sitrep/ingestion/protocol.py` | `Ingester` Protocol |
| `src/sitrep/ingestion/webhook.py` | Raw asyncio TCP webhook server |
| `src/sitrep/ingestion/severity.py` | `pre_score()` — arithmetic severity from SLO targets |
| `src/sitrep/correlation/engine.py` | `CorrelationEngine.correlate()` — orchestrates pipeline |
| `src/sitrep/correlation/temporal.py` | `group_temporal()` — windowed aggregation |
| `src/sitrep/correlation/topology.py` | `group_topology()` — dependency-aware grouping |
| `src/sitrep/correlation/changes.py` | `find_change_candidates()` — change attribution |
| `src/sitrep/correlation/dedup.py` | `deduplicate()` — signal deduplication |
| `src/sitrep/snapshot/generator.py` | `SnapshotGenerator.generate()` — token budget, priority, cache |
| `src/sitrep/snapshot/model.py` | `ModelInterface` — prompt assembly, response parsing, verdict output |
| `src/sitrep/snapshot/token.py` | `TokenEstimator` Protocol + `CharDivFourEstimator` |
| `src/sitrep/state.py` | `StateMachine` — WATCHING/ALERT/INCIDENT/DEGRADED |
| `src/sitrep/cli.py` | CLI: `sitrep serve`, `sitrep status`, `sitrep replay` |
| `pyproject.toml` | Package config with dependencies |
| `tests/conftest.py` | Shared fixtures |
| `tests/test_*.py` (13 files) | Unit tests for each module |

---

## Chunk 1: Foundation — Scenarios, Types, Store (Phase 2.0-2.2)

### Task 1: Package Setup + Scenario Fixtures + Core Types

**Files:**
- Create: `pyproject.toml`
- Create: `src/sitrep/__init__.py`
- Create: `src/sitrep/types.py`
- Create: `scenarios/synthetic/simple-causal-chain.yaml`
- Create: `scenarios/synthetic/cascading-failure.yaml`
- Create: `scenarios/synthetic/misleading-correlation.yaml`
- Create: `scenarios/synthetic/quiet-period.yaml`
- Create: `scenarios/synthetic/multi-candidate.yaml`
- Create: `tests/test_types.py`

**Phase 2.0 (fixtures) must come before any code — these are the test harness.**

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "sitrep"
version = "0.1.0a1"
description = "SitRep — Situational awareness through automated signal correlation"
requires-python = ">=3.11"
license = {text = "Apache-2.0"}
dependencies = [
    "pyyaml>=6.0.1",
    "structlog>=24.1",
    "anthropic>=0.39",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[project.scripts]
sitrep = "sitrep.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create the 5 scenario fixtures**

Create `scenarios/synthetic/simple-causal-chain.yaml`:
```yaml
scenario:
  id: "simple-causal-chain"
  description: "Single deploy causes latency spike on the same service"
  duration: "30m"
  topology:
    services:
      - name: payment-api
        tier: critical
        dependencies: []
        dependents: [checkout-service]
  events:
    - at: "T+0m"
      type: change
      payload:
        service: payment-api
        change_type: deploy
        detail: {from_version: "1.0.0", to_version: "1.1.0"}
    - at: "T+8m"
      type: alert
      payload:
        service: payment-api
        alert_name: latency_p99_breach
        value: 0.45
        threshold: 0.20
  expected_outcomes:
    correlation_groups: 1
    root_cause: "deploy v1.1.0"
    affected_services: [payment-api]
    change_candidates:
      - service: payment-api
        change_type: deploy
```

Create `scenarios/synthetic/cascading-failure.yaml`:
```yaml
scenario:
  id: "cascading-failure"
  description: "Deploy removes connection pooling, causing cascading latency"
  duration: "45m"
  topology:
    services:
      - name: payment-api
        tier: critical
        dependencies: [database-primary]
        dependents: [checkout-service]
      - name: checkout-service
        tier: critical
        dependencies: [payment-api]
      - name: database-primary
        tier: critical
  events:
    - at: "T+0m"
      type: change
      payload:
        service: payment-api
        change_type: deploy
        detail: {from_version: "2.3.0", to_version: "2.3.1"}
    - at: "T+12m"
      type: alert
      payload:
        service: payment-api
        alert_name: latency_p99_breach
        value: 0.52
        threshold: 0.20
    - at: "T+13m"
      type: metric_breach
      payload:
        service: payment-api
        metric: database_connection_pool_active
        value: 0
        expected_range: [10, 50]
    - at: "T+14m"
      type: alert
      payload:
        service: checkout-service
        alert_name: error_rate_breach
        value: 0.08
        threshold: 0.01
  expected_outcomes:
    correlation_groups: 1
    root_cause: "deploy v2.3.1"
    affected_services: [payment-api, checkout-service]
    change_candidates:
      - service: payment-api
        change_type: deploy
```

Create `scenarios/synthetic/misleading-correlation.yaml`:
```yaml
scenario:
  id: "misleading-correlation"
  description: "Two unrelated events happen at the same time"
  duration: "20m"
  topology:
    services:
      - name: auth-service
        tier: critical
        dependencies: []
        dependents: []
      - name: analytics-worker
        tier: low
        dependencies: []
        dependents: []
  events:
    - at: "T+0m"
      type: change
      payload:
        service: analytics-worker
        change_type: config
        detail: {key: "batch_size", from: "100", to: "500"}
    - at: "T+2m"
      type: alert
      payload:
        service: auth-service
        alert_name: error_rate_breach
        value: 0.05
        threshold: 0.01
  expected_outcomes:
    correlation_groups: 2
    affected_services: [auth-service, analytics-worker]
    false_correlation: true
```

Create `scenarios/synthetic/quiet-period.yaml`:
```yaml
scenario:
  id: "quiet-period"
  description: "Normal operations, no incidents"
  duration: "30m"
  topology:
    services:
      - name: api-gateway
        tier: critical
      - name: user-service
        tier: standard
  events:
    - at: "T+5m"
      type: quality_score
      payload:
        service: api-gateway
        agent: code-reviewer
        score: 0.92
    - at: "T+15m"
      type: quality_score
      payload:
        service: user-service
        agent: code-reviewer
        score: 0.88
  expected_outcomes:
    correlation_groups: 0
    affected_services: []
```

Create `scenarios/synthetic/multi-candidate.yaml`:
```yaml
scenario:
  id: "multi-candidate"
  description: "Three changes happened, only one caused the issue"
  duration: "30m"
  topology:
    services:
      - name: order-service
        tier: critical
        dependencies: [inventory-service, payment-api]
  events:
    - at: "T+0m"
      type: change
      payload:
        service: inventory-service
        change_type: deploy
        detail: {to_version: "3.1.0"}
    - at: "T+2m"
      type: change
      payload:
        service: order-service
        change_type: config
        detail: {key: "timeout_ms", from: "5000", to: "500"}
    - at: "T+3m"
      type: change
      payload:
        service: payment-api
        change_type: feature_flag
        detail: {flag: "new_checkout_flow", enabled: true}
    - at: "T+7m"
      type: alert
      payload:
        service: order-service
        alert_name: error_rate_breach
        value: 0.12
        threshold: 0.01
    - at: "T+8m"
      type: alert
      payload:
        service: order-service
        alert_name: latency_p99_breach
        value: 5.2
        threshold: 1.0
  expected_outcomes:
    correlation_groups: 1
    affected_services: [order-service]
    change_candidates:
      - service: order-service
        change_type: config
      - service: inventory-service
        change_type: deploy
      - service: payment-api
        change_type: feature_flag
```

- [ ] **Step 3: Write failing tests for core types**

```python
# tests/test_types.py
"""Tests for SitRep core data types."""
from __future__ import annotations

import pytest
from sitrep.types import (
    AgentState,
    ChangeCandidate,
    CorrelationGroup,
    EventType,
    SitRepEvent,
    TemporalGroup,
    TopologyCorrelation,
)


class TestSitRepEvent:
    def test_create_alert_event(self):
        event = SitRepEvent(
            id="evt-001",
            timestamp="2026-03-01T14:00:00Z",
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.8,
            payload={"alert_name": "latency_breach"},
        )
        assert event.service == "payment-api"
        assert event.type == EventType.ALERT
        assert event.severity == 0.8

    def test_default_fields(self):
        event = SitRepEvent(
            id="evt-002",
            timestamp="2026-03-01T14:00:00Z",
            source="github",
            type=EventType.CHANGE,
            service="api",
            environment="production",
            severity=0.5,
            payload={},
        )
        assert event.dependencies == []
        assert event.dependents == []
        assert event.ttl == 86400


class TestEventType:
    def test_all_types_exist(self):
        assert EventType.ALERT == "alert"
        assert EventType.METRIC_BREACH == "metric_breach"
        assert EventType.CHANGE == "change"
        assert EventType.QUALITY_SCORE == "quality_score"
        assert EventType.VERDICT == "verdict"
        assert EventType.CUSTOM == "custom"


class TestAgentState:
    def test_all_states(self):
        assert AgentState.WATCHING == "watching"
        assert AgentState.ALERT == "alert"
        assert AgentState.INCIDENT == "incident"
        assert AgentState.DEGRADED == "degraded"


class TestTemporalGroup:
    def test_create(self):
        group = TemporalGroup(
            service="payment-api",
            time_window=("2026-03-01T14:00:00Z", "2026-03-01T14:05:00Z"),
            events=[],
            count=3,
            peak_severity=0.9,
            duration_seconds=300.0,
        )
        assert group.count == 3
        assert group.peak_severity == 0.9


class TestCorrelationGroup:
    def test_create(self):
        group = CorrelationGroup(
            id="cg-001",
            priority=0,
            summary="3 alerts on payment-api",
            services=["payment-api"],
            signals=[],
            topology=None,
            change_candidates=[],
            first_seen="2026-03-01T14:00:00Z",
            last_updated="2026-03-01T14:05:00Z",
            event_count=3,
        )
        assert group.priority == 0
        assert group.topology is None
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd /Users/robfox/Documents/GitHub/opensrm-ecosystem/nthlayer-correlate && uv run pytest tests/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 5: Implement `src/sitrep/__init__.py` and `src/sitrep/types.py`**

Create `src/sitrep/__init__.py`:
```python
"""SitRep — Situational awareness through automated signal correlation."""
```

Create `src/sitrep/types.py` with all dataclasses from the spec:
- `EventType` enum (6 values)
- `SitRepEvent` dataclass (11 fields, `dependencies`/`dependents`/`ttl` have defaults)
- `TemporalGroup` dataclass
- `ChangeCandidate` dataclass
- `TopologyCorrelation` dataclass
- `CorrelationGroup` dataclass
- `AgentState` enum (4 values)

Follow the spec exactly. Use `from __future__ import annotations`, `@dataclass`, `field(default_factory=list)` for list defaults.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/robfox/Documents/GitHub/opensrm-ecosystem/nthlayer-correlate && uv run pytest tests/test_types.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/sitrep/__init__.py src/sitrep/types.py \
  scenarios/synthetic/*.yaml tests/test_types.py
git commit -m "feat: add scenario fixtures and core data types (Phase 2.0-2.1)"
```

---

### Task 2: EventStore Protocol + SQLite FTS5 Implementation

**Files:**
- Create: `src/sitrep/store/__init__.py`
- Create: `src/sitrep/store/protocol.py`
- Create: `src/sitrep/store/sqlite.py`
- Create: `tests/conftest.py`
- Create: `tests/test_store.py`

- [ ] **Step 1: Write failing tests for the SQLite store**

Key test cases:
- `TestInsertAndQuery`: insert event, query by time window, query with service filter, query with type filter, query with min_severity filter
- `TestSearch`: FTS5 full-text search across payload, search with service filter
- `TestTopology`: `get_by_topology()` returns events for service and its dependency neighbourhood
- `TestChanges`: `get_recent_changes()` returns only change-type events within window
- `TestExpiry`: insert events with short TTL, call `expire_old()`, verify deleted
- `TestStateHash`: same events → same hash, different events → different hash
- `TestBatchInsert`: `insert_batch()` writes multiple events atomically

Tests should create a `SitRepEvent` with all required fields. Use `tmp_path` fixture for the SQLite file.

`tests/conftest.py` should provide:
```python
@pytest.fixture
def sqlite_store(tmp_path):
    from sitrep.store.sqlite import SQLiteEventStore
    return SQLiteEventStore(str(tmp_path / "test-events.db"))

@pytest.fixture
def sample_alert_event():
    from sitrep.types import SitRepEvent, EventType
    return SitRepEvent(
        id="evt-test-001",
        timestamp="2026-03-01T14:12:00Z",
        source="prometheus",
        type=EventType.ALERT,
        service="payment-api",
        environment="production",
        severity=0.8,
        payload={"alert_name": "latency_p99_breach", "value": 0.52, "threshold": 0.20},
    )
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement `store/protocol.py` and `store/sqlite.py`**

`store/protocol.py` — the `EventStore` Protocol with 9 methods (from spec).

`store/sqlite.py` — `SQLiteEventStore` implementing:
- `__init__(db_path)`: create tables, FTS5 virtual table, indexes. WAL mode + busy_timeout=5000.
- Schema from spec: `events` table, `events_fts` FTS5 with Porter stemming, 5 indexes including partial index on changes.
- `insert()`: INSERT event, update FTS5 (`INSERT INTO events_fts(rowid, id, service, source, type, payload_text) SELECT rowid, id, service, source, type, ... FROM events WHERE id = ?`)
- `insert_batch()`: wrap multiple inserts in a transaction.
- `get_by_time_window()`: SELECT with WHERE clauses for optional filters.
- `search()`: `SELECT ... FROM events JOIN events_fts ON events.rowid = events_fts.rowid WHERE events_fts MATCH ? ORDER BY bm25(events_fts)`.
- `get_by_topology()`: query events for the service + its dependencies/dependents (parse JSON arrays).
- `get_recent_changes()`: filtered query on `type='change'` using the partial index.
- `expire_old()`: TTL-based — `DELETE FROM events WHERE julianday('now') - julianday(created_at) > ttl / 86400.0`. Also delete from FTS5. **Note:** The Protocol signature should be `def expire_old(self) -> int` (no `before_timestamp` param) since expiry is TTL-driven, not timestamp-driven.
- `get_state_hash()`: SHA256 of sorted event IDs in window.
- `get_stats()`: count, oldest/newest timestamp, db file size.

**FTS5 payload_text extraction**: Flatten the JSON payload dict into searchable text. E.g., `{"alert_name": "latency_breach", "value": 0.5}` → `"alert_name latency_breach value 0.5"`.

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add src/sitrep/store/ tests/conftest.py tests/test_store.py
git commit -m "feat: add EventStore protocol and SQLite FTS5 implementation (Phase 2.2)"
```

---

## Chunk 2: Ingestion + Correlation (Phase 2.3-2.4)

### Task 3: Severity Pre-Scoring + WebhookIngester

**Files:**
- Create: `src/sitrep/ingestion/__init__.py`
- Create: `src/sitrep/ingestion/protocol.py`
- Create: `src/sitrep/ingestion/severity.py`
- Create: `src/sitrep/ingestion/webhook.py`
- Create: `tests/test_severity.py`
- Create: `tests/test_ingestion.py`

- [ ] **Step 1: Write failing tests for severity pre-scoring**

```python
# tests/test_severity.py
from sitrep.ingestion.severity import pre_score
from sitrep.types import SitRepEvent, EventType

class TestPreScore:
    def test_with_slo_context(self):
        event = SitRepEvent(id="e1", timestamp="", source="prom", type=EventType.ALERT,
            service="api", environment="prod", severity=0.5,
            payload={"value": 0.52, "threshold": 0.20})
        slo = {"api": {"latency_p99": 0.20}}
        score = pre_score(event, slo)
        # (0.52 - 0.20) / 0.20 = 1.6 → capped at 1.0
        assert score == 1.0

    def test_without_slo_context(self):
        event = SitRepEvent(id="e2", timestamp="", source="prom", type=EventType.ALERT,
            service="api", environment="prod", severity=0.5, payload={})
        assert pre_score(event, None) == 0.5

    def test_below_threshold(self):
        event = SitRepEvent(id="e3", timestamp="", source="prom", type=EventType.ALERT,
            service="api", environment="prod", severity=0.5,
            payload={"value": 0.22, "threshold": 0.20})
        slo = {"api": {"latency_p99": 0.20}}
        score = pre_score(event, slo)
        # (0.22 - 0.20) / 0.20 = 0.1
        assert 0.0 < score < 0.5
```

- [ ] **Step 2: Write failing tests for WebhookIngester**

Test the ingester by mocking the store and posting JSON to it. Key tests:
- Valid event POST → stored, returns 200
- Missing required field → 400
- Event without `id` → auto-generated UUID
- Event without `timestamp` → auto-set to now

- [ ] **Step 3: Implement severity.py, protocol.py, webhook.py**

`ingestion/severity.py`:
```python
def pre_score(event: SitRepEvent, slo_targets: dict | None) -> float:
    if slo_targets is None or event.service not in slo_targets:
        return event.severity  # keep existing score (default 0.5)
    value = event.payload.get("value")
    threshold = event.payload.get("threshold")
    if value is None or threshold is None or threshold == 0:
        return event.severity
    return min(1.0, max(0.0, (value - threshold) / threshold))
```

`ingestion/protocol.py`: `Ingester` Protocol with `start()`, `stop()`, `on_event()`.

`ingestion/webhook.py`: Raw asyncio TCP server (same pattern as Arbiter's `WebhookAdapter`). Parse HTTP manually, validate JSON, call handler.

- [ ] **Step 4: Run all tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add src/sitrep/ingestion/ tests/test_severity.py tests/test_ingestion.py
git commit -m "feat: add severity pre-scoring and WebhookIngester (Phase 2.3)"
```

---

### Task 4: Deduplication + Temporal Grouping

**Files:**
- Create: `src/sitrep/correlation/__init__.py`
- Create: `src/sitrep/correlation/dedup.py`
- Create: `src/sitrep/correlation/temporal.py`
- Create: `tests/test_correlation_dedup.py`
- Create: `tests/test_correlation_temporal.py`

- [ ] **Step 1: Write failing tests for dedup**

Key tests:
- Two alerts with same `(source, service, alert_name, environment)` within window → collapsed to one with count=2
- Different services → not collapsed
- Different alert names → not collapsed

- [ ] **Step 2: Write failing tests for temporal grouping**

Key tests:
- Three alerts on same service within 5 min → one `TemporalGroup` with count=3
- Alerts on different services → separate groups
- Peak severity is max of all events in group
- Duration is time between first and last event

- [ ] **Step 3: Implement dedup.py and temporal.py**

`correlation/dedup.py`:
```python
def deduplicate(events: list[SitRepEvent],
                key_fields: list[str] | None = None) -> list[SitRepEvent]:
    """Collapse events with the same dedup key within the same temporal window.
    Default key: (source, service, type, environment) + alert_name from payload.
    Returns deduplicated list with count/duration in payload metadata.
    """
```

`correlation/temporal.py`:
```python
def group_temporal(events: list[SitRepEvent],
                   window_minutes: int = 5) -> list[TemporalGroup]:
    """Group events by service within time windows.
    Returns TemporalGroups with count, peak_severity, duration.
    """
```

- [ ] **Step 4: Run tests, verify pass**
- [ ] **Step 5: Commit**

```bash
git add src/sitrep/correlation/__init__.py src/sitrep/correlation/dedup.py \
  src/sitrep/correlation/temporal.py tests/test_correlation_dedup.py \
  tests/test_correlation_temporal.py
git commit -m "feat: add signal deduplication and temporal grouping (Phase 2.4 partial)"
```

---

### Task 5: Topology Grouping + Change Indexing + Correlation Engine

**Files:**
- Create: `src/sitrep/correlation/topology.py`
- Create: `src/sitrep/correlation/changes.py`
- Create: `src/sitrep/correlation/engine.py`
- Create: `tests/test_correlation_topology.py`
- Create: `tests/test_correlation_changes.py`
- Create: `tests/test_correlation_engine.py`

- [ ] **Step 1: Write failing tests for topology grouping**

Key tests:
- Service A depends on B, both have alerts → linked `TopologyCorrelation` with relationship
- Unrelated services → no topology link
- Missing topology data → graceful skip (returns empty)

- [ ] **Step 2: Write failing tests for change candidate indexing**

Key tests:
- Alert on service X, change on service X 8 minutes ago → `ChangeCandidate` with `same_service=True`, `temporal_proximity_seconds=480`
- Alert on service X, change on dependency Y → `ChangeCandidate` with `dependency_related=True`
- No recent changes → empty list
- Changes older than window → not included

- [ ] **Step 3: Write failing tests for the full correlation engine**

Test against scenario fixtures:
- Load `cascading-failure.yaml` events, feed to engine → expect 1 correlation group with 2 services and 1 change candidate
- Load `quiet-period.yaml` events → expect 0 correlation groups
- Load `misleading-correlation.yaml` → expect 2 separate correlation groups (unrelated services not merged)

- [ ] **Step 4: Implement topology.py, changes.py, engine.py**

`correlation/topology.py`:
```python
def group_topology(temporal_groups: list[TemporalGroup],
                   topology: dict | None) -> list[TopologyCorrelation]:
    """Cross-reference temporal groups against dependency topology.
    Links groups for services that have dependency relationships.
    """
```

`correlation/changes.py`:
```python
def find_change_candidates(store: EventStore,
                           temporal_groups: list[TemporalGroup],
                           topology: dict | None = None,
                           window_minutes: int = 30) -> dict[str, list[ChangeCandidate]]:
    """For each temporal group, find recent changes on the same service or dependencies.
    Returns mapping from service name to change candidates.
    """
```

`correlation/engine.py`:
```python
class CorrelationEngine:
    def correlate(self, store: EventStore, window_minutes: int = 5,
                  topology: dict | None = None,
                  slo_targets: dict | None = None) -> list[CorrelationGroup]:
        # 1. Query store for events in window
        # 2. Deduplicate
        # 3. Severity enrichment (second-pass pre-score)
        # 4. Temporal grouping
        # 5. Topology-aware grouping
        # 6. Change candidate indexing
        # 7. Assemble CorrelationGroups with priority scoring
```

Priority scoring: `priority = 0 (P0)` if peak_severity > 0.8 and service tier is critical; `1 (P1)` if severity > 0.6 or topology link to P0; `2 (P2)` if severity > 0.3; `3 (P3)` otherwise.

Template-generated summary: `"{count} alerts on {service} ({alert_names}) with {n} recent change(s) ({change_types})"`.

- [ ] **Step 5: Run all tests (including scenario-based), verify pass**

This is the **Demo 2 checkpoint** — the pre-correlation engine works end-to-end without a model.

- [ ] **Step 6: Commit**

```bash
git add src/sitrep/correlation/ tests/test_correlation_*.py
git commit -m "feat: add topology grouping, change indexing, and correlation engine (Phase 2.4)"
```

---

## Chunk 3: Snapshot + Model + State + CLI (Phase 2.5-2.7)

### Task 6: Token Estimator + Snapshot Generator

**Files:**
- Create: `src/sitrep/snapshot/__init__.py`
- Create: `src/sitrep/snapshot/token.py`
- Create: `src/sitrep/snapshot/generator.py`
- Create: `tests/test_snapshot_generator.py`

- [ ] **Step 1: Write failing tests for snapshot generator**

Key tests:
- Groups within budget → all included in prompt
- P3 group dropped when budget exceeded
- P0 group always included even if over budget
- Cache hit (same hash) → returns empty prompt + `cache_hit=True`
- State transition invalidates cache
- Max cache TTL expiry

- [ ] **Step 2: Implement token.py and generator.py**

`snapshot/token.py`: `TokenEstimator` Protocol + `CharDivFourEstimator` (from spec).

`snapshot/generator.py`: `SnapshotGenerator` with `generate()` method:
1. Compute content hash of sorted group IDs
2. Check cache (hash match + TTL + state unchanged)
3. Sort groups by priority
4. Apply token budget: include P0 always, then P1/P2/P3 until budget exhausted
5. Assemble prompt with system instructions, groups, service context
6. Return `(prompt, cache_hit)`

- [ ] **Step 3: Run tests, verify pass**
- [ ] **Step 4: Commit**

```bash
git add src/sitrep/snapshot/ tests/test_snapshot_generator.py
git commit -m "feat: add token estimator and snapshot generator with caching (Phase 2.5)"
```

---

### Task 7: Model Interface + Verdict Output

**Files:**
- Create: `src/sitrep/snapshot/model.py`
- Create: `tests/test_snapshot_model.py`

- [ ] **Step 1: Write failing tests for model interface**

Key tests (mock the Anthropic API call):
- Model returns valid JSON → parsed into per-correlation verdicts + parent verdict
- Model returns malformed JSON → graceful error, template-based fallback
- Degraded mode (model unavailable) → template verdicts with `confidence: 0.0`
- Verdict lineage: parent verdict has `lineage.children` linking to child verdicts
- Verdict fields: `producer.system = "sitrep"`, `subject.type = "correlation"`

- [ ] **Step 2: Implement model.py**

`snapshot/model.py`:
```python
class ModelInterface:
    def __init__(self, model: str = "claude-sonnet-4-20250514", max_tokens: int = 4096):
        ...

    async def interpret(self, prompt: str, verdict_store) -> list[Verdict]:
        """Call model, parse response into verdicts.
        Each correlation assessment → verdict.create(subject_type="correlation")
        Parent snapshot verdict links children via lineage.
        Returns list of all verdicts (children + parent).
        """

    def _build_system_prompt(self) -> str:
        """ZFC system prompt instructing model to:
        - Assess causal relationships for each correlation group
        - Assign confidence scores (0.0-1.0)
        - Recommend actions (flag/defer/escalate)
        - Recommend state transition if warranted (via tags)
        - Return structured JSON
        """

    def _parse_response(self, response_text: str, prompt_groups: list) -> list[dict]:
        """Parse model JSON response into verdict field dicts."""

    def _create_template_verdicts(self, groups: list[CorrelationGroup]) -> list[Verdict]:
        """Degraded mode: template-based verdicts with confidence 0.0."""
```

Use `verdict.create()` from the verdict library:
```python
from verdict import create as verdict_create, link as verdict_link
```

Per-correlation verdict:
```python
v = verdict_create(
    subject={"type": "correlation", "service": group.services[0],
             "ref": group.id, "summary": group.summary},
    judgment={"action": action, "confidence": confidence,
              "reasoning": reasoning, "tags": tags},
    producer={"system": "sitrep", "model": self.model_name},
)
```

Parent snapshot verdict with lineage:
```python
parent = verdict_create(
    subject={"type": "correlation", "service": None,
             "ref": cycle_id, "summary": f"Snapshot: {len(groups)} groups"},
    judgment={"action": overall_action, "confidence": overall_confidence,
              "reasoning": overall_reasoning, "tags": state_tags},
    producer={"system": "sitrep"},
)
parent.lineage.children = [v.id for v in child_verdicts]
```

- [ ] **Step 3: Run tests, verify pass**
- [ ] **Step 4: Commit**

```bash
git add src/sitrep/snapshot/model.py tests/test_snapshot_model.py
git commit -m "feat: add model interface with verdict output (Phase 2.6)"
```

---

### Task 8: Config + State Machine

**Files:**
- Create: `src/sitrep/config.py`
- Create: `src/sitrep/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write failing tests for state machine**

Key tests:
- Initial state is WATCHING
- P0 group → transitions to ALERT
- Multiple P1 groups → transitions to ALERT
- No P0/P1 for 10 minutes → transitions back to WATCHING
- External incident declaration → INCIDENT
- Model failure → DEGRADED
- DEGRADED + model recovery → WATCHING
- Each state returns correct interval (WATCHING=300s, ALERT=60s, INCIDENT=30s, DEGRADED=120s)
- Each state returns correct cache TTL

- [ ] **Step 2: Implement config.py and state.py**

`config.py`:
```python
@dataclass
class SitRepConfig:
    store_path: str = "sitrep-events.db"
    ingestion_host: str = "127.0.0.1"
    ingestion_port: int = 8081
    correlation_window_minutes: int = 5
    dedup_key_fields: list[str] = field(default_factory=lambda: ["source", "service", "type", "environment"])
    token_budget: int = 4000
    cache_ttl_minutes: int = 15
    model_name: str = "claude-sonnet-4-20250514"
    model_max_tokens: int = 4096
    verdict_store_path: str = "verdicts.db"
    manifests_dir: str | None = None
    watching_interval: int = 300
    alert_interval: int = 60
    incident_interval: int = 30
    degraded_interval: int = 120

def load_config(path: str | None = None) -> SitRepConfig:
    """Load from YAML file, with defaults for missing fields."""
```

`state.py`:
```python
class StateMachine:
    def __init__(self):
        self.state = AgentState.WATCHING
        self._last_p0_time: datetime | None = None
        self._last_p1_time: datetime | None = None

    def update(self, groups: list[CorrelationGroup],
               model_healthy: bool = True) -> AgentState:
        """Evaluate state transition based on correlation output.
        Returns the new state. Transitions are deterministic transport.
        """

    def get_interval(self) -> int:
        """Snapshot interval in seconds for current state."""

    def get_cache_ttl(self) -> int | None:
        """Cache TTL in seconds. None = no caching (INCIDENT)."""

    def declare_incident(self) -> None:
        """External incident declaration → INCIDENT state."""

    def resolve_incident(self) -> None:
        """Incident resolved → WATCHING state."""
```

- [ ] **Step 3: Run tests, verify pass**
- [ ] **Step 4: Commit**

```bash
git add src/sitrep/config.py src/sitrep/state.py tests/test_state.py
git commit -m "feat: add config loading and agent state machine (Phase 2.7 partial)"
```

---

### Task 9: CLI (serve, status, replay)

**Files:**
- Create: `src/sitrep/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests for CLI**

Key tests:
- `replay` with `cascading-failure.yaml` → reports correlation groups found and change candidates
- `replay` with `quiet-period.yaml` → reports 0 groups
- `replay` with `--no-model` flag → runs transport-only (no model call), still reports groups
- `status` in demo mode → shows WATCHING state
- Invalid scenario path → error exit code

- [ ] **Step 2: Implement cli.py**

Three subcommands via argparse:

**`serve`**: Load config → open stores → start ingester → run correlation loop on timer → call model → emit verdicts. Use `asyncio.run()` with signal handling for graceful shutdown.

**`status`**: Load config → open stores → print state, event count, last snapshot time, active groups.

**`replay`**:
1. Parse scenario YAML
2. Convert `"T+Nm"` timestamps to ISO 8601 (reference time: `2026-01-01T00:00:00Z`)
3. Insert events into an in-memory or temp SQLite store
4. Build topology from scenario's topology section
5. Run correlation engine
6. If `--no-model`: skip model, report transport results only
7. If model enabled: generate snapshot, call model, create verdicts
8. Compare results against `expected_outcomes`
9. Print report: groups found, change candidates, verdict count, accuracy vs expected

- [ ] **Step 3: Run tests, verify pass**
- [ ] **Step 4: Commit**

```bash
git add src/sitrep/cli.py tests/test_cli.py
git commit -m "feat: add CLI with serve, status, and replay commands (Phase 2.7)"
```

---

### Task 10: End-to-End Integration + Regression

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/robfox/Documents/GitHub/opensrm-ecosystem/nthlayer-correlate && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Run replay against all scenarios**

```bash
uv run sitrep replay --scenario scenarios/synthetic/simple-causal-chain.yaml --no-model
uv run sitrep replay --scenario scenarios/synthetic/cascading-failure.yaml --no-model
uv run sitrep replay --scenario scenarios/synthetic/misleading-correlation.yaml --no-model
uv run sitrep replay --scenario scenarios/synthetic/quiet-period.yaml --no-model
uv run sitrep replay --scenario scenarios/synthetic/multi-candidate.yaml --no-model
```

Expected: Each produces correct correlation group counts matching `expected_outcomes`.

- [ ] **Step 3: Run replay with model (integration test)**

```bash
uv run sitrep replay --scenario scenarios/synthetic/cascading-failure.yaml
```

Expected: Correlation verdicts produced with lineage. This is the accept criteria for Phase 2.

- [ ] **Step 4: Commit any fixes**

---

## Task Summary

| Task | Phase | Description | Key Files |
|------|-------|-------------|-----------|
| 1 | 2.0-2.1 | Package setup + scenario fixtures + core types | `pyproject.toml`, `types.py`, `scenarios/*.yaml` |
| 2 | 2.2 | EventStore protocol + SQLite FTS5 | `store/protocol.py`, `store/sqlite.py` |
| 3 | 2.3 | Severity + WebhookIngester | `ingestion/severity.py`, `ingestion/webhook.py` |
| 4 | 2.4a | Dedup + temporal grouping | `correlation/dedup.py`, `correlation/temporal.py` |
| 5 | 2.4b | Topology + changes + correlation engine | `correlation/topology.py`, `correlation/changes.py`, `correlation/engine.py` |
| 6 | 2.5 | Token estimator + snapshot generator | `snapshot/token.py`, `snapshot/generator.py` |
| 7 | 2.6 | Model interface + verdict output | `snapshot/model.py` |
| 8 | 2.7a | Config + state machine | `config.py`, `state.py` |
| 9 | 2.7b | CLI (serve, status, replay) | `cli.py` |
| 10 | — | End-to-end integration + regression | — |

## Demo Checkpoints

**Demo 2 (after Task 5):** Feed `cascading-failure.yaml` through the pre-correlation engine. Show: raw events → 1 correlation group with payment-api + checkout-service linked by topology, deploy v2.3.1 as change candidate. No model required.

**Full Phase 2 (after Task 10):** `sitrep replay --scenario scenarios/synthetic/cascading-failure.yaml` produces correlation verdicts with lineage in the shared verdict store.
