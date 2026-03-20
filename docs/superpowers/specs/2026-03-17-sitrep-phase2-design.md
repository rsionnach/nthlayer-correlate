# SitRep Phase 2 — Full Implementation Design

## Goal

Implement the SitRep pre-correlation agent end-to-end: event ingestion, indexed storage, pre-correlation engine, snapshot generation, model interface, verdict output, state machine, and CLI. Tier 1 only (WebhookIngester + SQLite FTS5). This is Phase 2 of the OpenSRM ecosystem implementation plan.

## Accept Criteria

`sitrep replay --scenario scenarios/synthetic/cascading-failure.yaml` produces correlation verdicts with lineage. `sitrep serve` starts the full pipeline (webhook ingestion + pre-correlation + snapshot generation + model calls + verdict output). `sitrep status` shows current agent state.

---

## Architecture

```
Event Sources → WebhookIngester → SQLite FTS5 EventStore → Pre-Correlation Engine
                                                                    ↓
                                                          CorrelationGroups
                                                                    ↓
                                                         Snapshot Generator
                                                    (token budget, priority tiers, cache)
                                                                    ↓
                                                           Model (judgment)
                                                                    ↓
                                                        Verdict Output (verdict library)
```

Every box above the model is transport. The model handles only the judgment that remains after transport has done everything it can. This is ZFC.

---

## Package Structure

```
sitrep/
├── scenarios/
│   └── synthetic/                    # Phase 2.0: test fixtures FIRST
│       ├── simple-causal-chain.yaml
│       ├── cascading-failure.yaml
│       ├── misleading-correlation.yaml
│       ├── quiet-period.yaml
│       └── multi-candidate.yaml
├── src/sitrep/
│   ├── __init__.py
│   ├── types.py                      # SitRepEvent, EventType, CorrelationGroup, etc.
│   ├── config.py                     # SitRepConfig loaded from sitrep.yaml
│   ├── store/
│   │   ├── __init__.py
│   │   ├── protocol.py               # EventStore Protocol
│   │   └── sqlite.py                 # SQLite FTS5 implementation
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── protocol.py               # Ingester Protocol
│   │   ├── webhook.py                # HTTP webhook server
│   │   └── severity.py               # Severity pre-scoring from SLO targets
│   ├── correlation/
│   │   ├── __init__.py
│   │   ├── engine.py                 # Orchestrates all correlation steps
│   │   ├── temporal.py               # Temporal grouping (windowed aggregation)
│   │   ├── topology.py               # Topology-aware grouping (manifest deps)
│   │   ├── changes.py                # Change candidate indexing
│   │   └── dedup.py                  # Signal deduplication
│   ├── snapshot/
│   │   ├── __init__.py
│   │   ├── generator.py              # Token budget, priority tiers, caching
│   │   ├── model.py                  # Model interface (prompt assembly, response parsing)
│   │   └── token.py                  # TokenEstimator protocol (len//4 for Tier 1)
│   ├── state.py                      # Agent state machine (WATCHING/ALERT/INCIDENT/DEGRADED)
│   └── cli.py                        # sitrep serve | status | replay
├── tests/
│   ├── conftest.py                   # Shared fixtures (store, scenarios)
│   ├── test_types.py
│   ├── test_store.py
│   ├── test_ingestion.py
│   ├── test_severity.py
│   ├── test_correlation_temporal.py
│   ├── test_correlation_topology.py
│   ├── test_correlation_changes.py
│   ├── test_correlation_dedup.py
│   ├── test_correlation_engine.py
│   ├── test_snapshot_generator.py
│   ├── test_snapshot_model.py
│   ├── test_state.py
│   └── test_cli.py
└── pyproject.toml
```

---

## Module Specifications

### types.py — Core Data Types

All types are `@dataclass`. No Pydantic. Follows the Verdict library pattern.

```python
class EventType(str, Enum):
    ALERT = "alert"
    METRIC_BREACH = "metric_breach"
    CHANGE = "change"
    QUALITY_SCORE = "quality_score"
    VERDICT = "verdict"
    CUSTOM = "custom"

@dataclass
class SitRepEvent:
    id: str                          # unique event ID
    timestamp: str                   # ISO 8601
    source: str                      # originating system
    type: EventType
    service: str                     # affected service name
    environment: str                 # production, staging, etc.
    severity: float                  # 0.0-1.0, pre-scored
    payload: dict[str, Any]          # source-specific data
    dependencies: list[str]          # services this one depends on (flattened from spec's topology.dependencies)
    dependents: list[str]            # services that depend on this one (flattened from spec's topology.dependents)
    ttl: int                         # seconds until expiry (default 86400)

@dataclass
class TemporalGroup:
    service: str
    time_window: tuple[str, str]     # (start, end) ISO 8601
    events: list[SitRepEvent]
    count: int
    peak_severity: float
    duration_seconds: float

@dataclass
class ChangeCandidate:
    change: SitRepEvent
    affected_service: str
    temporal_proximity_seconds: float
    same_service: bool
    dependency_related: bool

@dataclass
class TopologyCorrelation:
    primary_service: str
    related_services: list[dict]     # [{service, relationship, events}]
    topology_path: list[str]

@dataclass
class CorrelationGroup:
    id: str
    priority: int                    # 0=P0 (critical), 1=P1, 2=P2, 3=P3
    summary: str                     # template-generated, NOT model
    services: list[str]
    signals: list[TemporalGroup]
    topology: TopologyCorrelation | None
    change_candidates: list[ChangeCandidate]
    first_seen: str
    last_updated: str
    event_count: int

class AgentState(str, Enum):
    WATCHING = "watching"
    ALERT = "alert"
    INCIDENT = "incident"
    DEGRADED = "degraded"
```

### store/protocol.py — EventStore Interface

```python
class EventStore(Protocol):
    def insert(self, event: SitRepEvent) -> None: ...
    def insert_batch(self, events: list[SitRepEvent]) -> None: ...
    def get_by_time_window(self, start: str, end: str, *,
                           service: str | None = None,
                           event_type: EventType | None = None,
                           min_severity: float | None = None) -> list[SitRepEvent]: ...
    def search(self, query: str, *,
               limit: int = 100,
               time_window: tuple[str, str] | None = None,
               service: str | None = None) -> list[SitRepEvent]: ...
    def get_by_topology(self, service: str, hops: int = 1) -> list[SitRepEvent]: ...
    def get_recent_changes(self, service: str, window_minutes: int = 30) -> list[SitRepEvent]: ...
    def expire_old(self, before_timestamp: str) -> int: ...
    def get_state_hash(self, time_window: tuple[str, str]) -> str: ...
    def get_stats(self) -> dict[str, Any]: ...
```

### store/sqlite.py — SQLite FTS5 Implementation

Schema from SITREP-PRECORRELATION.md:
- `events` table with `id, timestamp, source, type, service, environment, severity, payload (JSON), dependencies (JSON), dependents (JSON), ttl, created_at`
- `events_fts` FTS5 virtual table with Porter stemming, BM25 ranking
- Indexes: `idx_events_timestamp`, `idx_events_service_time`, `idx_events_type_time`, `idx_events_changes` (partial on type='change'), `idx_events_expiry`
- WAL mode, busy timeout 5000ms (matches Verdict store pattern)
- `get_state_hash()`: SHA256 of sorted event IDs in window (for snapshot cache invalidation)
- `expire_old()`: deletes events where `created_at + ttl < now`

Default TTLs:
- Alerts/metric breaches: 24 hours (86400s)
- Changes: 7 days (604800s)
- Quality scores: 30 days (2592000s)
- Verdicts: 30 days (2592000s)

### ingestion/protocol.py — Ingester Interface

```python
class Ingester(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def on_event(self, handler: Callable[[SitRepEvent], Awaitable[None]]) -> None: ...
```

Every ingester calls the same handler. The handler writes events to the EventStore. The pre-correlation engine doesn't know which ingester produced the event.

### ingestion/webhook.py — WebhookIngester

HTTP server accepting POST requests with event payloads. Uses the same raw asyncio TCP server pattern as the Arbiter's WebhookAdapter (no framework dependency).

- Accepts JSON POST to `/events` endpoint
- Validates required fields: `source`, `type`, `service`, `payload`
- Generates `id` if not provided (UUID)
- Sets `timestamp` to now if not provided
- Calls `severity.pre_score()` if SLO targets available
- Writes to EventStore via `insert()`
- Returns 200 on success, 400 on validation error, 503 if store unavailable

### ingestion/severity.py — Severity Pre-Scoring

```python
def pre_score(event: SitRepEvent, slo_targets: dict | None) -> float:
    """Pre-score severity using SLO targets. Pure arithmetic.
    severity = min(1.0, (current_value - target_value) / target_value)
    Returns 0.5 if no SLO context available.
    """
```

### correlation/engine.py — Pre-Correlation Engine

Orchestrates all correlation steps. Runs on a timer (frequency determined by agent state). All operations are deterministic transport.

```python
class CorrelationEngine:
    def correlate(self, store: EventStore, window_minutes: int = 5,
                  topology: dict | None = None,
                  slo_targets: dict | None = None) -> list[CorrelationGroup]:
        """Run full pre-correlation pipeline:
        1. Query store for events in window
        2. Deduplicate (dedup.py)
        3. Severity enrichment — events without SLO-based severity
           get a second-pass pre-score using manifest SLO targets
           (ingestion/severity.py). This catches events that arrived
           without SLO context at ingestion time.
        4. Temporal grouping (temporal.py)
        5. Topology-aware grouping (topology.py) — if manifest available
        6. Change candidate indexing (changes.py)
        7. Assemble CorrelationGroups with priority scoring
        """
```

### correlation/temporal.py — Temporal Grouping

Events within a configurable time window affecting the same service are grouped. Multiple alerts about the same service within 5 minutes become a single `TemporalGroup` with count, duration, and peak severity.

### correlation/topology.py — Topology-Aware Grouping

Cross-references events against service dependency topology from OpenSRM manifests. If service A depends on service B and both have alerts in the same window, they're linked with the dependency direction noted. Requires topology data (optional — correlation works without it, just less precise).

### correlation/changes.py — Change Candidate Indexing

For each temporal group with elevated severity, queries the store for recent changes on the same service or its dependencies. Produces `ChangeCandidate` objects with computed temporal proximity. This is index lookup + arithmetic, not causal reasoning.

### correlation/dedup.py — Signal Deduplication

Dedup key: `(source, service, alert_name_or_metric, environment)`. Events with the same key within the same temporal window are collapsed to a single entry with count and duration.

### snapshot/generator.py — Snapshot Generator

Takes correlation groups, applies token budget and priority tiers, manages cache.

```python
class SnapshotGenerator:
    def generate(self, groups: list[CorrelationGroup],
                 previous_hash: str | None = None,
                 state: AgentState = AgentState.WATCHING,
                 service_context: dict | None = None
                 ) -> tuple[str, bool]:
        """Returns (model_prompt, cache_hit).
        If cache_hit is True, model_prompt is empty and the cached
        snapshot should be reused. Otherwise, model_prompt contains
        the assembled prompt for the model interface.
        """
```

**Token budget** (default 4000 tokens):
- 500 reserved for system prompt
- 500 reserved for trend/history context
- 3000 available for correlation groups
- Groups sorted by priority, included until budget exhausted
- Low-priority groups dropped entirely (never truncated)
- P0 groups always included even if budget exceeded

**Priority tiers:**
| Tier | Criteria |
|------|----------|
| P0 | severity > 0.8 AND service tier is critical |
| P1 | severity > 0.6 OR topology correlation with P0 |
| P2 | severity > 0.3 |
| P3 | everything else |

**Caching:**
- Compute SHA256 content hash of sorted correlation group IDs
- If hash matches previous cycle → return cached snapshot, no model call
- State transition (→ ALERT, → INCIDENT) always invalidates cache
- Max cache TTL: 15 minutes (configurable)

### snapshot/model.py — Model Interface

The ZFC judgment boundary.

**Input to model:**
- Correlation groups (structured, pre-prioritised, within token budget)
- Previous snapshot (for differential assessment)
- Service context from manifests (SLO targets, tiers, topology)
- Trend context (recent incident history)

**Output from model (parsed into verdicts):**
- Per-correlation assessment: causal judgment, confidence, recommended action
- Overall situation assessment: severity, state transition recommendation
- Recommended actions

**Verdict output:**
- Each correlation assessment → `verdict.create(subject_type="correlation", judgment_action="flag|watch|escalate")`
- Parent snapshot verdict → links children via `lineage.children`
- State transition recommendation → encoded as tag `state_transition:<state>` on parent verdict, read by transport layer

**Degraded mode (model unavailable):**
- Template-based verdicts with `confidence: 0.0`
- Reasoning: `"template-based, model unavailable"`
- Pre-correlation continues (transport runs without model)

### snapshot/token.py — Token Estimation

```python
class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...

class CharDivFourEstimator:
    """len(text) // 4 — good enough for Tier 1."""
    def estimate(self, text: str) -> int:
        return len(text) // 4
```

### state.py — Agent State Machine

Four states with deterministic transition rules:

```
WATCHING ──(P0 or multiple P1 groups)──→ ALERT
WATCHING ──(model calls failing)──→ DEGRADED
ALERT ──(external incident declaration)──→ INCIDENT
ALERT ──(no P0/P1 groups for 10 min)──→ WATCHING
ALERT ──(model calls failing)──→ DEGRADED
INCIDENT ──(incident resolved)──→ WATCHING
DEGRADED ──(model calls recovering)──→ WATCHING
```

State determines:
| State | Snapshot Frequency | Model Tier | Cache TTL |
|-------|-------------------|------------|-----------|
| WATCHING | 5 min | Standard | 15 min |
| ALERT | 1 min | Frontier | 5 min |
| INCIDENT | 30 sec | Frontier | No cache |
| DEGRADED | 2 min | Standard | 10 min |

State transitions are driven by pre-correlation output (transport), not model judgment. P0 group → ALERT happens immediately without waiting for model opinion. Model can recommend transitions via verdict tags, but transport makes the decision.

### cli.py — CLI

Three subcommands:

**`sitrep serve [--config sitrep.yaml]`** — Start the full pipeline:
1. Load config
2. Open EventStore (SQLite)
3. Start WebhookIngester (HTTP server)
4. Start correlation loop (timer-based, frequency from state)
5. On each cycle: correlate → generate snapshot → call model → emit verdicts
6. Run until SIGTERM/SIGINT

**`sitrep status [--config sitrep.yaml]`** — Show current state:
- Agent state (WATCHING/ALERT/INCIDENT/DEGRADED)
- Event store stats (event count, oldest/newest, size)
- Last snapshot time and cache status
- Active correlation groups

**`sitrep replay --scenario <path> [--config sitrep.yaml]`** — Replay a scenario:
1. Load scenario YAML
2. Feed events through ingestion → store → correlation engine
3. Generate snapshot, call model (or mock), produce verdicts
4. Compare against expected outcomes
5. Report: correlation groups found, accuracy vs expected, verdict count

### config.py — Configuration

```yaml
# sitrep.yaml
store:
  backend: sqlite
  path: sitrep-events.db

ingestion:
  host: 127.0.0.1
  port: 8081

correlation:
  window_minutes: 5
  dedup_key_fields: [source, service, type, environment]

snapshot:
  token_budget: 4000
  cache_ttl_minutes: 15

model:
  provider: anthropic
  model: claude-sonnet-4-20250514
  max_tokens: 4096

verdict:
  store:
    backend: sqlite
    path: verdicts.db

topology:
  manifests_dir: null    # path to OpenSRM manifests (optional)

state:
  watching_interval_seconds: 300
  alert_interval_seconds: 60
  incident_interval_seconds: 30
  degraded_interval_seconds: 120
```

---

## Scenario Fixture Schema

Fixtures built FIRST (Phase 2.0), before any SitRep code:

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
        detail: { from_version: "2.3.0", to_version: "2.3.1" }

    - at: "T+12m"
      type: alert
      payload:
        service: payment-api
        alert_name: latency_p99_breach
        value: 0.52
        threshold: 0.20

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

Five fixtures covering: simple causal chain, cascading failure, misleading temporal correlation (unrelated), quiet period, and multi-candidate (3 changes, 1 cause).

**Relative timestamp parsing:** The `at: "T+12m"` format is relative to a reference time (T+0m). During replay, `T+0m` is set to a fixed reference time (e.g., `2026-01-01T00:00:00Z`). The replay engine parses `T+Nm` by extracting the integer N and adding N minutes to the reference. This produces deterministic ISO 8601 timestamps for each event.

---

## Verdict Integration

SitRep depends on the `verdict` library (path-based: `pip install -e ../verdicts`).

**Output:** Each correlation assessment → `verdict.create()` with:
- `subject.type = "correlation"`
- `producer.system = "sitrep"`
- `judgment.action = "flag" | "watch" | "escalate"`
- `judgment.confidence = 0.0-1.0`

Parent snapshot verdict links children via `lineage.children`.

**Self-measurement:** `verdict.accuracy(producer="sitrep", subject_type="correlation")` measures correlation accuracy. Human overrides resolve verdicts as confirmed/overridden, feeding the calibration loop.

**Shared store:** Single `verdicts.db` with WAL mode, shared with Arbiter (per IMPLEMENTATION-PLAN R4). Cross-component lineage queries work because all verdicts are in one store.

---

## Dependencies

- Python 3.11+
- `verdict` library (path-based dependency)
- `anthropic` SDK (for model calls)
- `pyyaml` (config loading)
- `structlog` (structured logging, matching ecosystem convention)
- No web framework (raw asyncio TCP for webhook, matching Arbiter pattern)
- No numpy/scipy (pure stdlib arithmetic)

---

## What's NOT in This Design

- NATS/Kafka ingesters (Tier 2+)
- PostgreSQL/ClickHouse stores (Tier 2+)
- Differential snapshots (optimization after caching works)
- OTel metric emission (Phase 4)
- Contract manifest validation (ECOSYSTEM-GAPS)
- Grafana dashboards (NthLayer generates later)
- Polling ingester (nice-to-have, not required for Tier 1)
- API for querying snapshots (CLI + verdict store queries are sufficient for Phase 2)

---

## Relationship to Other Specs

| Spec | What This Design Implements |
|------|---------------------------|
| SITREP-PRECORRELATION.md | Items 1-10 from implementation priority (event schema through state machine). Item 11 partial (scenario replay yes, contract manifest deferred). Items 12-14 deferred. |
| IMPLEMENTATION-PLAN.md | Phase 2.0-2.7 in full. Demo 2 checkpoint at 2.5. |
| VERDICT-INTEGRATION.md | Section 3 (SitRep verdict output). |
| ECOSYSTEM-GAPS.md | Scenario replay (Gap 1, basic). Contract manifest (Gap 2) deferred. Degradation behavior (Gap 3) for SitRep. |
| sitrep/ZFC.md | Transport/judgment boundary enforced throughout. |
