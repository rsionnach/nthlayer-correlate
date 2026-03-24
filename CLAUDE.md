# nthlayer-correlate — Agent Context

Situational awareness through automated signal correlation. Continuously pre-correlates observability signals in the background so a correlated picture is ready before an incident is declared.

**Status: Phase 2 Tier 1 fully implemented. Tier 1 scope: WebhookIngester + SQLite FTS5.**

---

<!-- AUTO-MANAGED: build-commands -->
## Build Commands

- **Install nthlayer-learn library (prerequisite):** `pip install -e ../../nthlayer-learn/lib/python`
- **Run tests:** `uv run pytest tests/ -v`
- **Run single test file:** `uv run pytest tests/test_types.py -v`
- **Run CLI:** `uv run nthlayer-correlate serve | status | replay`
- **TDD workflow:** write failing test → implement → `uv run pytest` verify pass → commit
- **Commit style:** `feat: <description> (Phase X.Y)`
<!-- END AUTO-MANAGED -->

---

## What This Is

nthlayer-correlate solves the signal correlation problem at enterprise scale: millions of events per minute across metrics, logs, traces, alerts, change events, and quality scores. Rather than querying raw events at incident time (too slow, too noisy), nthlayer-correlate pre-correlates continuously so the correlated view is built before anyone asks for it. When an incident fires, generating a situational snapshot takes seconds rather than minutes of ad-hoc querying across Prometheus, Loki, Jaeger, and change history.

nthlayer-correlate is one component in the OpenSRM ecosystem (opensrm, nthlayer-measure, nthlayer, nthlayer-respond) but is designed to stand alone. A team without the rest of the ecosystem can adopt nthlayer-correlate for signal correlation.

---

## Core Design Principle: ZFC

**Zero Framework Cognition** — draw a hard line between transport and judgment.

**Transport (code handles this):**
- Ingesting events from the streaming layer
- Grouping signals by service and time window
- Maintaining the rolling pre-correlation index
- Computing temporal proximity between signals
- Generating the structured snapshot schema
- Publishing snapshots via API and SSE

**Judgment (model handles this):**
- Interpreting what correlations mean
- Assessing whether a temporal correlation is likely causal
- Generating the natural language summary
- Recommending actions
- Deciding the snapshot severity level

Pre-correlation is transport (deterministic grouping, windowing, counting). Interpreting what the correlations mean is judgment. Never put causal reasoning in code.

---

<!-- AUTO-MANAGED: architecture -->
## Architecture

### Pre-Correlation Concept

nthlayer-correlate continuously runs in the background, grouping related signals by service, time window, and topology. The pre-correlated data is indexed and ready for snapshot generation at any time. Every box before the model call is transport; the model handles only the judgment that remains after transport has done everything it can.

### Package Structure (Phase 2 — Tier 1)

```
src/nthlayer_correlate/
├── types.py          # SitRepEvent, EventType, TemporalGroup, ChangeCandidate,
│                     # TopologyCorrelation, CorrelationGroup, AgentState — all @dataclass, no Pydantic
├── config.py         # SitRepConfig loaded from sitrep.yaml
├── store/
│   ├── protocol.py   # EventStore Protocol (insert, insert_batch, get_by_time_window, search, get_by_topology, get_recent_changes, expire_old, get_state_hash, get_stats)
│   └── sqlite.py     # SQLite FTS5, WAL mode, BM25 ranking, Porter stemming
├── ingestion/
│   ├── protocol.py   # Ingester Protocol (async start/stop; on_event handler: Callable[[SitRepEvent], Union[Awaitable[None], None]])
│   ├── webhook.py    # Raw asyncio TCP HTTP server, POST /events — no framework
│   └── severity.py   # Severity pre-scoring from SLO targets (pure arithmetic)
├── correlation/
│   ├── engine.py     # CorrelationEngine.correlate(store, window_minutes, topology, slo_targets): dedup → severity enrichment → temporal → topology → changes → priority scoring; two-pass assembly (topology-linked groups first)
│   ├── temporal.py   # Window-based grouping → TemporalGroup (count, peak_severity, duration)
│   ├── topology.py   # Cross-reference events against OpenSRM manifest deps
│   ├── changes.py    # ChangeCandidate indexing — index lookup + arithmetic, not causal reasoning
│   └── dedup.py      # Dedup key: source|service|type|environment[|alert_name_or_metric]
├── snapshot/
│   ├── generator.py  # Token budget, priority tiers P0-P3, SHA256 content hash caching
│   ├── model.py      # ZFC judgment boundary — prompt assembly, response parsing, degraded mode
│   └── token.py      # TokenEstimator Protocol; CharDivFourEstimator: len(text) // 4
├── state.py          # AgentState machine — deterministic transport-driven transitions
└── cli.py            # sitrep serve | status | replay
```

Tier 1 uses SQLite FTS5 + WebhookIngester only. NATS/Kafka and PostgreSQL/ClickHouse are Tier 2+.

### Agent States

| State | Snapshot Interval | Model Tier | Cache TTL |
|-------|-------------------|------------|-----------|
| **WATCHING** | 5 min | Standard | 15 min |
| **ALERT** | 1 min | Frontier | 5 min |
| **INCIDENT** | 30 sec | Frontier | No cache |
| **DEGRADED** | 2 min | Standard | 10 min |

State transitions are driven by pre-correlation output (transport), not model judgment. P0 group → ALERT fires immediately without waiting for a model opinion. Model can recommend transitions via verdict tags (`state_transition:<state>`), but transport makes the final decision.

Transition rules:
- WATCHING → ALERT: P0 group present, or multiple P1 groups
- WATCHING → DEGRADED: model calls failing
- ALERT → INCIDENT: external incident declaration
- ALERT → WATCHING: no P0/P1 groups for 10 min
- INCIDENT → WATCHING: incident resolved
- DEGRADED → WATCHING: model calls recovering

### Snapshot Token Budget

Default budget: 4000 tokens total
- 500 reserved for system prompt
- 500 reserved for trend/history context
- 3000 available for correlation groups

Priority tiers:
| Tier | Criteria |
|------|----------|
| P0 | severity > 0.8 AND service tier is critical — always included, even if budget exceeded |
| P1 | severity > 0.6 OR topology correlation with P0 |
| P2 | severity > 0.3 |
| P3 | everything else — dropped entirely, never truncated |

Cache: SHA256 of sorted correlation group IDs; invalidated on state transition; max TTL 15 min.

### EventStore TTLs (SQLite)

| Event type | TTL |
|------------|-----|
| Alerts / metric breaches | 24h (86400s) |
| Changes | 7 days (604800s) |
| Quality scores / verdicts | 30 days (2592000s) |

### Situational Snapshot Schema

```yaml
snapshot:
  id: sitrep-2026-03-06T14:23:00Z
  triggered_by: alert | schedule | manual
  window: 15m
  severity: info | warning | critical
  summary: "model-generated natural language summary"
  signals:
    - source: arbiter
      type: quality_degradation
      detail: "worker rejection rate 0.33 (threshold 0.20)"
      timestamp: 2026-03-06T14:18:00Z
  correlations:
    - signals: [0, 1]
      confidence: 0.82
      interpretation: "quality degradation started within 7m of model version change"
  topology:
    affected_services: [webapp, api-gateway]
    dependency_chain: [webapp -> api-gateway -> database]
  recommended_actions:
    - "investigate model version change on rig-webapp"
```

Signals and topology are transport. Summary, correlation interpretation, and recommended actions are judgment (model-generated).

### Configuration (sitrep.yaml)

```yaml
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

### CLI Commands

- `nthlayer-correlate serve [--config sitrep.yaml]` — start full pipeline (WebhookIngester + correlation loop)
- `nthlayer-correlate status [--config sitrep.yaml]` — show agent state, store stats, last snapshot, active groups
- `nthlayer-correlate replay --scenario <path> [--config sitrep.yaml] [--no-model]` — feed scenario YAML into a temp SQLite store; runs correlation sub-steps manually (bypasses `engine.correlate()` to handle historical scenario timestamps); sub-step order: dedup → severity enrichment → temporal grouping → topology grouping → change_candidates → `_assemble_groups` (uses `CorrelationEngine` helper for group assembly); prints group summary; optionally calls model for verdicts

### Scenario Fixtures (Phase 2.0 — built first)

Five fixtures in `scenarios/synthetic/`: `simple-causal-chain`, `cascading-failure`, `misleading-correlation`, `quiet-period`, `multi-candidate`.

Schema: `scenario.id`, `description`, `duration`, `topology` (services with tier/deps/dependents), `events` (at T+Xm relative to `2026-01-01T00:00:00Z`, type, payload), `expected_outcomes` (correlation_groups count, root_cause, affected_services, change_candidates, false_correlation).

Scenario coverage:
- `simple-causal-chain` — deploy then alert on same service; 1 group expected
- `cascading-failure` — deploy removes connection pooling; multi-service cascade; 1 group expected
- `misleading-correlation` — coincident events on unrelated services (no topology link); 2 groups, `false_correlation: true`
- `multi-candidate` — 3 changes before alert; tests disambiguation; all 3 surfaced as change candidates
- `quiet-period` — quality_score events only; 0 groups expected; verifies no false positives during normal operations

### Scenario Test Coverage (`tests/test_correlation_engine.py`)

`TestCorrelationEngineWithScenarios` drives all five fixtures end-to-end against a real `SQLiteEventStore` and `CorrelationEngine`:

| Test | Scenario | Key assertion |
|------|----------|---------------|
| `test_cascading_failure` | cascading-failure | `len(groups) >= 1`; all `expected_outcomes.affected_services` present; `change_candidates` non-empty |
| `test_quiet_period` | quiet-period | Zero elevated groups (`priority <= 2`) — quality_score events must not produce false P0-P2 groups |
| `test_misleading_correlation` | misleading-correlation | No single group contains both `auth-service` and `analytics-worker` |
| `test_simple_causal_chain` | simple-causal-chain | `>= 1` group; payment-api present; deploy change candidate surfaced (`change_type == "deploy"`) |
| `test_multi_candidate` | multi-candidate | All 3 change candidates surfaced; `change_types` set validated against expected |

Helper `_estimate_severity` logic used when loading scenarios (converts event payload to severity float):
- `quality_score`: `1.0 - score` (high score = low severity)
- `alert` / `metric_breach`: `abs(value - threshold) / threshold`, clamped to [0.0, 1.0]
- `change`: fixed `0.1` (informational)
- default: `0.5`

The `quiet-period` test filters to `priority <= 2` groups only — this is the correct definition of "elevated" (P0/P1/P2); P3 groups may still be returned without constituting a false positive.

### Signal Sources

- OTel metrics and traces via OTel Collector (Prometheus remote write, OTLP)
- Alerts from Alertmanager (webhook)
- Change events normalised via OpenSRM change event schema (GitHub, ArgoCD, LaunchDarkly, model registries, prompt management systems)
- Quality scores from nthlayer-measure (OTel metrics)
- Deployment records from CI/CD pipelines

### Streaming Layer (Tier 2+)

- **Enterprise:** Kafka, partitioned by service, topics by signal type
- **Smaller deployments:** NATS for lighter-weight message routing
- Tier 1 uses only WebhookIngester (raw asyncio TCP, no streaming dependency)

### Change Attribution

Identifying candidate causes is transport (index lookup + arithmetic on pre-computed proximity). Evaluating whether a temporal correlation is causal is judgment (model decides). `correlation/changes.py` produces `ChangeCandidate` objects with `temporal_proximity_seconds`, `same_service`, `dependency_related` — never causal verdicts.

`get_recent_changes(service, window_minutes, reference_time=None)` accepts an optional `reference_time` (ISO 8601). When set, the lookback window is anchored to that timestamp instead of wall-clock now — required for replay with historical scenario data. `find_change_candidates` passes `group.time_window[0]` as `reference_time` so replay finds the correct change candidates.

Change event schema includes AI-specific change types: model version swaps, prompt changes, LoRA adapter deployments, context window configuration changes — alongside traditional change types (deploys, config, feature flags, schema migrations).
<!-- END AUTO-MANAGED -->

---

## Verdict Integration

Verdict output is fully specified in Phase 2 (see `docs/superpowers/specs/2026-03-17-sitrep-phase2-design.md`). nthlayer-correlate depends on the `nthlayer-learn` library (path-based: `pip install -e ../../nthlayer-learn/lib/python`).

**Output:** Each correlation assessment → `verdict.create()` with:
- `subject.type = "correlation"`, `producer.system = "sitrep"`
- `judgment.action = "flag" | "watch" | "escalate"`, `judgment.confidence = 0.0-1.0`
- Parent snapshot verdict links children via `lineage.children`

**Ingestion:** `verdict` is a valid `EventType` alongside `alert`, `metric_breach`, `change`, `quality_score`. nthlayer-measure quality verdicts arrive via the same ingestion path and participate in pre-correlation.

**Degraded mode:** Template-based verdicts with `confidence: 0.0` and `reasoning: "template-based, model unavailable"` — transport continues, judgment pauses (ZFC fail-open pattern).

**Shared store:** Single `verdicts.db` (SQLite WAL) shared with nthlayer-measure. Cross-component lineage queries work because all verdicts are in one store.

**Self-measurement:** `verdict.accuracy(producer="sitrep", subject_type="correlation")` measures correlation accuracy. Human corrections feed the calibration loop via `gen_ai.override.*` OTel events.

---

## Self-Measurement

nthlayer-correlate has its own judgment SLOs, measured through the nthlayer-measure's governance framework:

- **Correlation accuracy:** What percentage of nthlayer-correlate's "related change" assessments do humans agree with?
- **False positive rate:** How often does nthlayer-correlate flag a change as incident-related when it isn't?

Every correlation assessment emits a `gen_ai.decision.*` OTel event. Human disagreements emit `gen_ai.override.*` events. If nthlayer-correlate's correlation quality drops, the nthlayer-measure's governance layer can reduce nthlayer-correlate's confidence levels or flag it for human review.

---

## OpenSRM Integration

nthlayer-correlate reads service topology from OpenSRM manifests to understand dependency relationships. A quality drop in service A that depends on service B triggers nthlayer-correlate to check service B's signals automatically. The manifest provides the dependency graph that makes topology-aware correlation possible.

OpenSRM integration is additive — nthlayer-correlate can correlate signals without manifests, but topology-aware correlation requires them.

---

## Ecosystem

| Component | Role |
|-----------|------|
| [opensrm](../opensrm/) | Shared manifest spec and change event schema |
| [nthlayer-learn](../nthlayer-learn/) | Data primitive — nthlayer-correlate correlation output becomes verdicts; nthlayer-measure quality verdicts ingested as events |
| [nthlayer-measure](../nthlayer-measure/) | Quality scores consumed by nthlayer-correlate; governs nthlayer-correlate's own judgment SLOs |
| [nthlayer](../nthlayer/) | Topology exports that nthlayer-correlate uses for dependency-aware correlation |
| [nthlayer-correlate](../nthlayer-correlate/) | This repo — pre-correlation and situational snapshots |
| [nthlayer-respond](../nthlayer-respond/) | Consumes nthlayer-correlate snapshots as starting context for incident response |

Each component works independently. Composition happens through shared OpenSRM manifests and OTel conventions.

---

## Contributing

- Fork, create feature branch from `main`, make changes, run tests, open PR
- Follows OpenSRM specification for manifest formats, semantic conventions, telemetry standards
- Follows ZFC: transport (ingesting signals, grouping, windowing, counting) belongs in code; judgment (interpreting correlations, assessing causal relationships, recommending actions) belongs to the model
- Issue templates: `.github/ISSUE_TEMPLATE/bug_report.md` and `.github/ISSUE_TEMPLATE/feature_request.md`
- Contributions licensed under Apache License 2.0

---

## What Not to Build

- Do not put causal reasoning logic in code. Temporal proximity is transport; causality assessment is judgment.
- Do not hardcode correlation thresholds. They come from config or OpenSRM manifests.
- Do not couple snapshot generation to a specific streaming technology. The streaming layer is pluggable (NATS or Kafka).
- Do not generate snapshots synchronously at query time for enterprise-scale deployments. Pre-correlation must happen continuously in the background.
