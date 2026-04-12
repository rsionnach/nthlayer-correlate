# nthlayer-correlate — Agent Context

Situational awareness through automated signal correlation. Continuously pre-correlates observability signals in the background so a correlated picture is ready before an incident is declared.

**Status: Phase 2 Tier 1 fully implemented. Tier 1 scope: WebhookIngester + SQLite FTS5.**

---

<!-- AUTO-MANAGED: build-commands -->
## Build Commands

- **Install dependencies:** `uv sync --extra dev`
- **Install nthlayer-learn (local path):** `pip install -e ../../nthlayer-learn/lib/python`
- **Run tests:** `uv run pytest tests/ -v`
- **Run tests (CI flags):** `uv run pytest tests/ -v --tb=short -x`
- **Run single test file:** `uv run pytest tests/test_types.py -v`
- **Run linting:** `uv run ruff check src/ tests/ --ignore E501,B008,F841,B007,E402,E721,E722,B012,I001`
- **Run security scan (non-blocking):** `uv pip install pip-audit && uv run pip-audit --progress-spinner off`
- **Run CLI:** `uv run nthlayer-correlate serve | status | replay | correlate`
- **TDD workflow:** write failing test → implement → `uv run pytest` verify pass → commit
- **Commit style:** `feat: <description> (Phase X.Y)`
- **CI:** pushes/PRs to `main` or `develop`; matrix Python 3.11 and 3.12; uses `uv sync --extra dev --no-sources` — nthlayer-learn and nthlayer-common resolve from PyPI (not local paths) in CI
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
│   ├── model.py      # ZFC judgment boundary — prompt assembly, response parsing (uses nthlayer_common.parsing), degraded mode; serves `serve`/`replay` subcommands
│   └── token.py      # TokenEstimator Protocol; CharDivFourEstimator: len(text) // 4
├── state.py          # AgentState machine — deterministic transport-driven transitions
├── prometheus.py     # fetch_alerts, fetch_metric_breaches, verdict_to_event, load_dependency_graph, blast_radius_services
├── reasoning.py      # Reasoning layer — LLM causal analysis between group assembly and verdict creation; uses nthlayer_common.parsing for clamp/strip_markdown_fences; serves `correlate` subcommand
├── notifications.py  # build_correlation_blocks(), find_slack_thread_ts() — Slack Block Kit builders
├── traces/
│   ├── __init__.py   # Trace backend adapters package
│   ├── protocol.py   # TraceBackend Protocol + dataclasses: TraceSpanSummary, ServiceCallEdge, ErrorSummary,
│   │                 # OperationLatency, TopologyDivergence, ServiceTraceProfile, TraceEvidence
│   ├── tempo.py      # TempoTraceBackend — Grafana Tempo adapter; TraceQL metrics API + search;
│   │                 # optional Prometheus service graphs; 5-phase get_trace_evidence: stats → edges → operations → errors → samples;
│   │                 # internal _ServiceStats(p50_ms, p95_ms, p99_ms, total_count, error_count, error_rate);
│   │                 # internal _EdgeAccumulator(count, durations: list[float], errors) — accumulates client span edges;
│   │                 # helpers: _extract_metric_by_service (averages samples, promLabels fallback),
│   │                 # _extract_metric_by_service_and_op, _parse_service_graph_results (seconds→ms),
│   │                 # _span_to_summary, _traceql_service_filter (static, || for multi-service),
│   │                 # _parse_tempo_timestamp (nanoseconds→datetime UTC); latency ns÷1_000_000→ms;
│   │                 # _query_operation_breakdown: 5 parallel TraceQL calls faceted by (service, operation),
│   │                 #   sorted by p99_ms descending, capped at 10 per service, baseline comparison via change_pct;
│   │                 # _query_edges_from_traces: slow path, client spans (span.peer.service as target),
│   │                 #   skips self-calls and spans missing span.peer.service, limit=1000;
│   │                 # aclose() releases httpx client connections
│   └── topology.py   # detect_topology_divergence(trace_evidence, declared_deps) → TopologyDivergence;
│                     # blast-radius-scoped: edges to/from services outside blast radius are ignored
└── cli.py            # serve | status | replay | correlate; _build_parser() public (for testability);
                      # _proximity_confidence() helper + _PROXIMITY_WINDOW_SECONDS=1800.0 constant
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
traces:
  backend: null          # "tempo" | null (disabled)
  detail: full           # "full" | "summary"
  baseline_window: 1h    # baseline comparison window
  tempo:
    endpoint: "http://localhost:3200"
    org_id: ""           # X-Scope-OrgID for multi-tenant Tempo
    timeout_seconds: 30
    use_service_graphs: true  # false = derive edges from raw traces
```

### CLI Commands

- `nthlayer-correlate serve [--config sitrep.yaml]` — start full pipeline (WebhookIngester + correlation loop); event queue maxsize=10000, drained single-threaded into SQLiteEventStore before each correlation cycle; VerdictStore opened with try/except fail-open (logs `verdict_store_not_available` if nthlayer-learn unavailable); handles SIGTERM/SIGINT
- `nthlayer-correlate status [--config sitrep.yaml]` — show agent state, store stats (event_count, min/max timestamp, DB file size); `--store-dir` flag overrides DB path (used in tests)
- `nthlayer-correlate replay --scenario <path> [--config sitrep.yaml] [--no-model]` — feed scenario YAML into a `tempfile.TemporaryDirectory` (auto-cleaned on exit); runs correlation sub-steps manually (bypasses `engine.correlate()` to handle historical scenario timestamps); sub-step order: dedup → severity enrichment → temporal grouping → topology grouping → change_candidates → `_assemble_groups` (uses `CorrelationEngine` helper for group assembly); `window_minutes = max(event_spread_minutes + 1, 5)`; `find_change_candidates` called with `window_minutes + 30`; topology loaded via `_build_topology_dict()` from YAML `topology.services[]`; prints group summary; optionally calls model for verdicts
- `nthlayer-correlate correlate --trigger-verdict <id> --prometheus-url <url> --specs-dir <dir> --verdict-store <path> [--reasoning|--no-reasoning] [--model <provider/model>] [--decision-store <path>] [--trace-backend tempo] [--tempo-endpoint <url>] [--trace-detail full|summary]` — live correlation triggered by a verdict from nthlayer-measure; fetches Prometheus alerts + metric breaches for blast-radius services; runs reasoning layer (model-based causal analysis if API key set, heuristic fallback otherwise); writes a `correlation` verdict (`producer.system="nthlayer-correlate"`) to the shared verdict store; `metadata.custom` includes `reasoning_mode: "model"|"heuristic"`, `reasoning: <result>|null`, `evidence_sources: {prometheus, verdict_store, trace_backend: "<backend>"|null}`, and `trace_query_time_ms: <float>|null`; after writing correlation verdict, sends Slack notification when `SLACK_WEBHOOK_URL` set — calls `find_slack_thread_ts(verdict_store, [trigger_verdict_id])` to thread under the breach message, stores returned `ts` in `corr_verdict.metadata.custom["slack_thread_ts"]`; returns exit 0 on success, 1 if trigger verdict not found. `--no-reasoning` disables model call entirely (heuristic only). `--model` accepts `"provider/model"` format (e.g. `"openai/gpt-4o"`, `"anthropic/claude-sonnet-4-20250514"`). Heuristic fallback uses `_proximity_confidence(seconds)` — linear decay from 1.0 (simultaneous) to 0.0 at `_PROXIMITY_WINDOW_SECONDS=1800.0` (30 min); returns 0.5 for unknown proximity. `--trace-backend`: optional; `"tempo"` activates `TempoTraceBackend`; `--tempo-endpoint` overrides `SitRepConfig.tempo_endpoint`; `--trace-detail` defaults to `"full"`. `--decision-store <path>`: optional; when set, writes a content-addressed Verdict record via `nthlayer_common.records.verdict_bridge.build_decision_verdict` (agent="correlate"; action includes `root_causes[:3]`, `blast_radius_count`, `trace_backend: "<backend>"|null`, `trace_services_count: int`; summaries: technical="Correlation: {service}, {N} groups, {M} blast radius[, trace evidence from {backend}]", plain=verdict_summary[:280], executive="{service} correlation — {mode}[ + traces]"); fail-open (logs `decision_verdict_write_failed` on error). `correlate_command()` also accepts `trace_backend: object | None = None` (injectable TraceBackend Protocol instance, used in tests and programmatic callers) and `trace_baseline_window: str = "1h"` (parsed via `_parse_duration()`); trace errors degrade gracefully (logs `trace_evidence_unavailable`); calls `trace_backend.aclose()` after gather if present; topology divergence computed via `detect_topology_divergence` when trace evidence present. Parser exposed as `_build_parser()` for test access.

### Slack Notifications (`notifications.py`)

`src/nthlayer_correlate/notifications.py` — block builders and lineage walker for Slack threading.

**`build_correlation_blocks(verdict) -> tuple[list[dict], str]`**
- Extracts: `subject.ref` (service), `metadata.custom["root_causes"][0]["service"]`, `metadata.custom["blast_radius"]`, `judgment.confidence`
- Block format: header "🔍 ROOT CAUSE IDENTIFIED · {service}", body with root cause service/blast radius count/up to 5 blast services, context footer with "nthlayer-correlate · confidence X.XX · {verdict.id}"
- Returns `(blocks, fallback_text)`

**`find_slack_thread_ts(verdict_store, verdict_ids: list[str]) -> str | None`**
- Walks verdict lineage to find `slack_thread_ts` set by nthlayer-measure on the original breach verdict
- For each verdict ID: checks `metadata.custom["slack_thread_ts"]`; if absent, walks `lineage.context` one hop up and checks those verdicts too
- Returns first `slack_thread_ts` found, or `None` (graceful degradation — no thread = top-level message)
- Exceptions on individual verdicts are swallowed — entire function never raises

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

### Model Interface Test Coverage (`tests/test_snapshot_model.py`)

`TestModelInterface` covers the ZFC judgment boundary in `snapshot/model.py`:

| Test | Key assertion |
|------|---------------|
| `test_successful_model_call_produces_verdicts` | 2 verdicts (1 child + 1 parent); child has correct action/confidence/producer; parent links child via `lineage.children` |
| `test_model_failure_returns_template_verdicts` | 3 verdicts (2 children + 1 parent); all `confidence=0.0`; `reasoning` contains "template-based"; tag "degraded" present |
| `test_verdict_lineage_parent_links_children` | Parent `lineage.children` contains all child IDs |
| `test_escalate_bubbles_to_parent` | Any child `action="escalate"` → parent `action="escalate"` |
| `test_invalid_action_defaults_to_flag` | Action `"watch"` (not in valid set) → defaults to `"flag"` |
| `test_confidence_clamped` | Confidence `1.5` → clamped to `1.0` |
| `test_malformed_json_falls_back_to_template` | Non-JSON response → all verdicts `confidence=0.0` |
| `test_verdicts_stored_when_store_provided` | `mock_store.put` called 2× (child + parent) |

### Prometheus / Correlate Command Test Coverage (`tests/test_correlate_command.py`)

Covers the live `correlate` subcommand and `prometheus.py` module:

| Test | Key assertion |
|------|---------------|
| `test_load_dependency_graph` | Parses SRM YAML; builds forward deps and reverse `dependents` |
| `test_load_dependency_graph_empty` | Returns `{}` for empty dir |
| `test_blast_radius_includes_trigger_and_dependents` | Includes trigger, its dependents, and its dependencies |
| `test_blast_radius_leaf_service` | Leaf service still pulls in its dependencies |
| `test_verdict_to_event` | Produces `EventType.VERDICT`; `payload["verdict_id"]` and `payload["breach"]` set correctly |
| `test_correlate_command_writes_correlation_verdict` | Full integration with mocked Prometheus; correlation verdict written with `lineage.context` containing trigger ID; `subject.ref=service`, `producer.system="nthlayer-correlate"` |
| `test_correlate_command_missing_verdict` | Returns exit code 1 when trigger verdict not found |
| `test_correlate_with_trace_backend` | `evidence_sources["trace_backend"] == "tempo"` and `trace_query_time_ms == approx(6240.0)` |
| `test_correlate_without_trace_backend` | `evidence_sources["trace_backend"] is None` when no backend configured |
| `test_trace_backend_connect_error_degrades` | `ConnectError` → result==0, `evidence_sources["trace_backend"] is None` |
| `test_trace_backend_timeout_degrades` | `asyncio.TimeoutError` → result==0, `evidence_sources["trace_backend"] is None` |
| `test_decision_record_includes_trace_context` | Decision record action dict includes `trace_backend` and `trace_services_count`; technical summary contains "trace evidence from tempo" |

Helpers: `_make_trace_evidence()` builds minimal `TraceEvidence` with one `ServiceTraceProfile` (fraud-detect, `latency_change_pct=178.0`, `error_rate=0.02`); `_setup_trigger_and_mocks()` creates trigger verdict + mock alert/breach coroutines.

Mocking: `fetch_alerts` and `fetch_metric_breaches` patched via `unittest.mock.patch` on `nthlayer_correlate.prometheus`.

### CLI and Config Test Coverage (`tests/test_cli.py`)

In addition to `TestParseRelativeTime`, `TestReplayCommand`, and `TestStatusCommand`:

| Test class | Key assertions |
|------------|----------------|
| `TestConfigTraces` | `traces.backend/detail/baseline_window` parsed; `traces.tempo.endpoint/org_id/timeout_seconds/use_service_graphs` parsed; defaults correct when `traces:` section absent |
| `TestCLITraceFlags` | `_build_parser()` accepts `--trace-backend`, `--tempo-endpoint`, `--trace-detail`; all three flags are optional (absent → `trace_backend=None`, `tempo_endpoint=None`, `trace_detail="full"`) |

### Trace Protocol Test Coverage (`tests/test_trace_protocol.py`)

Covers all dataclasses in `traces/protocol.py`:

| Test class | Key assertions |
|------------|----------------|
| `TestTraceSpanSummary` | All fields set; optional `error_message` and `parent_service` accept `None` |
| `TestServiceCallEdge` | `source_service`, `target_service`, `request_count`, `error_count` set correctly |
| `TestErrorSummary` | `count`, `first_seen/last_seen`, `sample_trace_id=None` |
| `TestOperationLatency` | `baseline_p50_ms` and `change_pct` accept `None`; populated correctly when set |
| `TestTopologyDivergence` | Empty lists accepted |
| `TestServiceTraceProfile` | All latency percentiles, baseline, change_pct, error fields set correctly |

### Trace Tempo Test Coverage (`tests/test_trace_tempo.py`)

Covers `TempoTraceBackend`:

| Test class | Key assertions |
|------------|----------------|
| `TestConstructor` | Defaults: endpoint `http://localhost:3200`, `org_id=""`, `use_service_graphs=True`, `prometheus_url=None`; explicit params override; `org_id` sets `X-Scope-OrgID` header; no header when `org_id` empty; `TEMPO_ENDPOINT` env var respected |
| `TestHealthCheck` | Returns `True` on 200; `False` on non-200; `False` on connection error |
| `TestTraceQLMetricsQuery` | GET to `/api/metrics/query_range`; `q` and `step` params sent correctly |
| `TestTraceQLSearch` | GET to `/api/search`; `limit` param sent as string |
| `TestPrometheusQuery` | GET to `/api/v1/query`; empty dict returned when `prometheus_url=None` |
| `TestExtractMetricByService` | Single series averages all samples to per-service float; multiple series; empty response; `promLabels` fallback when `labels` missing service key |
| `TestExtractMetricByServiceAndOp` | Faceted response keyed by `(service, operation)` tuple |
| `TestParseServiceGraphResults` | Parses count/error/p99/p50 from Prometheus result format; converts seconds→ms (×1000) |
| `TestSpanToSummary` | Error span: `status="error"`, `error_message` from `span.status_message`; ok span: `status="ok"`, `error_message=None` |
| `TestServiceFilter` | Single service → equality expression; multiple services → `||`-joined expressions |
| `TestParseTempoTimestamp` | Nanoseconds integer → `datetime` with `tzinfo=timezone.utc` |
| `TestQueryServiceStats` | 5 parallel TraceQL calls (p50/p95/p99/count/error); converts ns→ms; skips services with zero total_count |
| `TestQueryOperationBreakdown` | 12 ops → capped at 10, sorted by p99_ms descending; baseline comparison: 5th call is baseline p50, `change_pct == approx(200.0)` for 50ms baseline vs 150ms incident |
| `TestQueryEdgesFromTraces` | 2 client spans A→B → 1 edge (count=2, errors=1); self-calls (A→A) produce no edges; spans missing `span.peer.service` skipped |
| `TestQueryServiceGraphsFromPrometheus` | 4 parallel Prometheus queries fired; edges parsed correctly from count/error/p99/p50 results |

### Trace Topology Test Coverage (`tests/test_trace_topology.py`)

Covers `detect_topology_divergence`:

| Test | Key assertion |
|------|---------------|
| `test_no_divergence` | Declared A→B matches observed A→B: both lists empty |
| `test_declared_not_observed` | Declared A→B but no trace edge: `("A","B")` in `declared_not_observed` |
| `test_observed_not_declared` | Traces show A→C but not in specs: `("A","C")` in `observed_not_declared` |
| `test_mixed_divergence` | Match (A→B) not in either list; observed-only (A→D) in `observed_not_declared`; declared edge to C (not in blast radius profiles) NOT flagged as `declared_not_observed` |
| `test_only_blast_radius_compared` | Declared edge to service outside blast radius is ignored (no false `declared_not_observed`) |
| `test_empty_trace_evidence` | No profiles → both lists empty |

### Reasoning Layer Test Coverage (`tests/test_reasoning.py`)

| Test class | Key assertions |
|------------|----------------|
| `TestDegradedReasoning` | `degraded=True`, `confidence=0.0`, reason string propagated to `overall_assessment` and per-group `reasoning` |
| `TestParseReasoningResponse` | Valid JSON parsed; markdown fences stripped; unknown group IDs filtered; confidence clamped to [0.0, 1.0]; malformed JSON raises `JSONDecodeError` |
| `TestBuildUserPrompt` | DEPENDENCY GRAPH section present; group details (ID, services, change candidates) included; SLO TARGETS section present when provided; topology block included for multi-service groups |
| `TestBuildSystemPrompt` | Contains "dependency", "temporal", "cascading", "JSON" |
| `TestReasoningAvailable` | Returns `True` with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `NTHLAYER_MODEL` set; `False` without any |
| `TestReasonAboutCorrelations` | Successful call → `degraded=False`, correct confidence; empty groups → degraded; API error → degraded; malformed response → degraded; timeout → degraded |
| `TestCorrelateCommandReasoning` | `--no-reasoning` → `reasoning_mode=heuristic`, `reasoning=None`; API key absent → heuristic fallback; API key present + model success → `reasoning_mode=model`, confidence from model, `overall_assessment` in `subject.summary` |

### Reasoning Layer (`reasoning.py`)

Sits between `CorrelationEngine.correlate()` group assembly and verdict creation. Calls an LLM to assess causal relationships. Additive — `--no-reasoning` produces identical verdict structure to pre-reasoning behavior.

**Serves:** `correlate` subcommand (live Prometheus-triggered). See also: `snapshot/model.py` which serves `serve` and `replay` subcommands.

**Public API:**
- `reasoning_available() -> bool` — returns `True` if `NTHLAYER_MODEL`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` is set (keyless providers like Ollama need only `NTHLAYER_MODEL`)
- `reason_about_correlations(groups, dependency_graph, slo_targets=None, trace_evidence=None, model=None, max_tokens=4096, timeout=30) -> dict` — calls LLM via `nthlayer_common.llm.llm_call`; `trace_evidence` is an optional `TraceEvidence` object passed through to `_build_user_prompt`; `model=None` defers to `NTHLAYER_MODEL` env var; accepts `"provider/model"` format when supplied; falls back to `_degraded_reasoning()` on any exception

**Result structure:**
```python
{
    "groups": [{"group_id", "root_cause", "confidence", "reasoning", "recommended_actions", "is_causal", "degraded"}],
    "overall_assessment": str,
    "overall_confidence": float,   # clamped to [0.0, 1.0] via nthlayer_common.parsing.clamp
    "degraded": bool,
}
```

**Degraded mode:** `confidence=0.0`, `degraded=True`, `root_cause=None` — transport continues, judgment pauses (ZFC fail-open). Triggered by: no groups, API error, malformed JSON response, timeout.

**Provider routing:** delegates to `nthlayer_common.llm.llm_call` via `asyncio.to_thread`. Model format `"provider/model"` — provider selection (anthropic, openai, ollama, etc.) handled by the shared wrapper in `nthlayer-common`.

**Response parsing:** `_parse_reasoning_response` uses `nthlayer_common.parsing.strip_markdown_fences` to clean model output before `json.loads`, and `nthlayer_common.parsing.clamp` to normalise confidence values.

**Prompt structure:** System prompt — reliability engineer persona, JSON-only output format, causal reasoning rules (dependency direction, temporal proximity, cascading failure patterns). User prompt — DEPENDENCY GRAPH block, optional SLO TARGETS block, per-group detail (signals capped at 5 events each, change candidates, topology). `_compact_payload` truncates large dict values and long lists to stay within token budget. `_build_user_prompt` caps groups at MAX_GROUPS=10 (sorted by priority, P3 dropped first) and prunes the dependency graph to services present in the selected groups + 1 hop of deps and dependents before building the DEPENDENCY GRAPH section — reduces prompt token cost for large topologies.

### Prometheus Module (`prometheus.py`)

Used by the live `correlate` command. Transport only — no causal reasoning.

- `fetch_alerts(client, prometheus_url, services)` — firing alerts from `/api/v1/alerts`, filtered to given services → `list[SitRepEvent]`
- `fetch_metric_breaches(client, prometheus_url, services, window_minutes=30)` — three PromQL checks per service:
  - `slo:error_budget:ratio{service="X"}` breach if `< 0`
  - `slo:http_request_duration_seconds:p99{service="X"}` breach if `> 0.5s`
  - `service:http_errors:rate5m{service="X"}` breach if `> 0.01` (1%)
- `verdict_to_event(verdict)` — converts an nthlayer-learn verdict to `SitRepEvent(type=VERDICT)`
- `load_dependency_graph(specs_dir)` — loads OpenSRM YAML specs → `dict[service → {tier, dependencies, dependents}]`; also builds reverse-dependency `dependents` list
- `blast_radius_services(trigger_service, graph)` — walks dependents (upstream consumers) AND dependencies (downstream services) of the trigger service; returns affected set covering both directions
- `_alert_severity(label)` — maps Prometheus severity label to float: `critical`→0.95, `warning`→0.6, `info`→0.3, default→0.5
- `_query_instant(client, prometheus_url, query)` — async PromQL instant query; returns `float | None`; returns `None` on HTTP error, empty result, or NaN

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

<!-- AUTO-MANAGED: prompts -->
## Prompt Definitions (`prompts/`)

YAML-based prompt definitions — migration from hardcoded Python strings to versioned YAML files complete. Both `reasoning.py` and `snapshot/model.py` load prompts from YAML via `nthlayer_common.prompts.load_prompt`.

**YAML structure:** each file has `name`, `version`, `system` (with `{schema_block}` placeholder injected at load time by `load_prompt`), `response_schema` (JSON Schema for the expected response), and `user_template` (with `{{ variable }}` placeholders interpolated at call time).

**Wiring:** `_build_system_prompt()` in both modules follows this pattern:
```python
_PROMPT_PATH = Path(__file__).parent... / "prompts" / "reasoning.yaml"

def _build_system_prompt() -> str:
    spec = load_prompt(_PROMPT_PATH)
    return spec.system
```

| File | Serves | Key schema fields |
|------|--------|-------------------|
| `prompts/reasoning.yaml` | `correlate` subcommand (`reasoning.py`) | `groups[].{group_id, root_cause, confidence, reasoning, recommended_actions, is_causal}`, `overall_assessment`, `overall_confidence` |
| `prompts/snapshot.yaml` | `serve`/`replay` subcommands (`snapshot/model.py`) | `assessments[].{group_id, service, summary, action, confidence, reasoning, tags}`; action enum: `flag\|defer\|escalate` |
<!-- END AUTO-MANAGED -->

---

## Verdict Integration

Verdict output is fully specified in Phase 2 (see `docs/superpowers/specs/2026-03-17-sitrep-phase2-design.md`). nthlayer-correlate depends on the `nthlayer-learn` library (path-based: `pip install -e ../../nthlayer-learn/lib/python`) and `nthlayer-common` (path-based: `../nthlayer-common`, installed via `uv sync`).

**Output:** Each correlation assessment → `verdict.create()` with:
- `subject.type = "correlation"`, `producer.system = "nthlayer-correlate"`
- `judgment.action = "flag" | "escalate" | "defer"`, `judgment.confidence = 0.0-1.0`; invalid actions default to `"flag"`; confidence clamped to `[0.0, 1.0]`
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
