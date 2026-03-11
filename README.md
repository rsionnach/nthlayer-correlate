# SitRep

**Situational awareness through automated signal correlation.**

[![Status: Architecture](https://img.shields.io/badge/Status-Architecture-blue?style=for-the-badge)](https://github.com/rsionnach/sitrep)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-green?style=for-the-badge)](LICENSE)

Enterprise-scale distributed systems produce an enormous volume of observability signals: metrics at 15-second intervals across thousands of services, structured logs on every request, distributed traces, alerts from multiple monitoring systems, change events from CI/CD pipelines, feature flag changes, and infrastructure scaling events. This is millions of events per minute. No human can correlate across all of these signals during an incident by reading dashboards, and no existing tool pre-processes these signals into a form that AI agents (or humans) can consume efficiently.

SitRep solves this by continuously pre-correlating signals in the background so that when something goes wrong, the correlated picture is already built. Rather than querying raw events at incident time (which is too slow and too noisy at scale), SitRep groups related signals, computes temporal proximity, identifies co-occurring changes, and maintains a rolling window of pre-correlated state. When an incident happens, generating a situational snapshot takes seconds rather than minutes of ad-hoc querying across Prometheus, Loki, Jaeger, and your change history.

This project is in the architecture phase. The design is documented below, and implementation has not yet started.

---

## The Problem

Prometheus handles metrics. Loki handles logs. Jaeger handles traces. But correlating across all three plus change events plus quality scores at enterprise scale is an unsolved problem that most teams handle manually during incidents (or don't handle at all). A company running thousands of services (think SaaS platforms like Workday, Stripe, Twilio) needs something that continuously watches across signal types and has the correlated view ready before anyone asks for it.

Agentic systems add additional volume on top of the enterprise baseline. AI agent decisions, quality scores, model version changes, prompt updates, and adapter deployments all produce signals that existing observability infrastructure wasn't designed to handle. SitRep exists because raw observability data at enterprise scale is unusable without a pre-correlation layer.

---

## Pre-Correlation

This is the core architectural concept. Pre-correlation is the difference between "let me spend 20 minutes querying four different systems during an incident" and "here's what happened, already correlated, in 3 seconds."

SitRep continuously runs in the background, grouping related signals by service, time window, and topology. When a metric anomaly appears near a deployment event for a related service, SitRep has already noted the temporal proximity before anyone asks. The pre-correlated data is indexed and ready for snapshot generation at any time.

Pre-correlation itself is transport (deterministic grouping, windowing, counting). Interpreting what the correlations mean is judgment (the model decides whether a temporal correlation is likely causal). This follows [Zero Framework Cognition](ZFC.md).

---

## Situational Snapshots

A snapshot is a point-in-time document that answers "what's happening right now?" with structured evidence. Every snapshot follows the same schema regardless of how it was triggered:

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
      detail: "worker ace-mjxwfy7e rejection rate 0.33 (threshold 0.20)"
      timestamp: 2026-03-06T14:18:00Z
    - source: otel
      type: deploy
      detail: "model version updated on rig-webapp 12m ago"
      timestamp: 2026-03-06T14:11:00Z
  correlations:
    - signals: [0, 1]
      confidence: 0.82
      interpretation: "quality degradation started within 7m of model version change"
  topology:
    affected_services: [webapp, api-gateway]
    dependency_chain: [webapp -> api-gateway -> database]
  recommended_actions:
    - "investigate model version change on rig-webapp"
    - "check if other workers on same rig are affected"
```

The schema captures what happened (signals), what's related (correlations with confidence scores), what's affected (topology from OpenSRM manifests), and what to do next (recommended actions). The signals and topology sections are transport (structured data from known sources). The summary, correlation interpretation, and recommended actions are judgment (model-generated).

---

## Event Ingestion

At enterprise scale, SitRep needs a streaming/queuing layer between event producers and the correlation engine. Raw events from OTel collectors, monitoring systems, CI/CD pipelines, change event sources, and quality score producers flow through a message queue that handles backpressure, replay, and fan-out.

- **Enterprise scale:** Kafka, with partitioning by service and topics by signal type. Kafka's compaction and replay capabilities are designed for exactly this volume, and its consumer group model maps naturally to having multiple ecosystem components (SitRep, the Arbiter, Mayday) each consuming the same event stream independently.
- **Smaller deployments:** NATS provides a lighter-weight alternative for teams that don't need Kafka's full feature set.

SitRep consumes from the queue, pre-correlates, and stores the results. This decouples event production rate from correlation processing rate, which is essential when thousands of services are each producing metrics, logs, and traces continuously.

---

## Generation Modes

SitRep generates snapshots in three modes, each producing the same schema but with different urgency and depth:

- **Batch (periodic):** Lightweight summaries every N minutes (default: 5 minutes in WATCHING state) for continuous situational awareness. These snapshots capture the ambient state of the system.
- **Incident-triggered:** On alert firing, SitRep pulls in more context and performs deeper correlation. These snapshots are richer and more detailed, designed to give an incident responder (human or agent) immediate context.
- **Refresh (on-demand):** When a human or agent requests an updated picture, SitRep generates a fresh snapshot incorporating any new information that arrived since the last one. During active incidents, refresh snapshots run on a 1-minute cycle.

---

## Agent States

SitRep operates in distinct states that affect its behaviour:

| State | Trigger | Behaviour |
|-------|---------|-----------|
| **WATCHING** | Normal operations | Background correlation, 5-minute snapshot cycle |
| **ALERT** | Elevated signal detected | Increased correlation frequency, broader signal ingestion |
| **INCIDENT** | Incident declared | Continuous reassessment, 1-minute snapshots, pushes context to Mayday |
| **DEGRADED** | Own judgment SLO metrics below threshold | Conservative mode, reduced confidence in correlations, flags for human review |

The DEGRADED state is important: SitRep monitors its own quality and reduces confidence when it detects its correlations are less reliable. This is self-awareness as a feature, not an afterthought.

---

## Change Attribution

When quality degrades (signalled by the Arbiter), SitRep looks for recent changes that temporally correlate with the degradation. It consumes changes via the standardised change event schema defined in the [OpenSRM spec](https://github.com/rsionnach/opensrm), which means all change sources (deploys, config updates, model version swaps, prompt changes, adapter deployments, formula revisions) arrive in a uniform format:

```yaml
change_event:
  id: chg-2026-03-06-001
  timestamp: "2026-03-06T14:11:00Z"
  type: model_version
  scope:
    service: webapp
    environment: production
    rig: rig-webapp
  source: model-registry
  actor: deploy-pipeline
  detail:
    from_version: "claude-sonnet-4-20250514"
    to_version: "claude-sonnet-4-20250715"
  risk: low
  rollback_available: true
```

SitRep doesn't need per-source integrations because the change event schema normalises everything. The pre-correlation layer continuously maintains a rolling window of changes, so when a quality signal fires, the candidate causes are already indexed. Identifying the candidate set is transport (pre-computed by the correlation engine). Evaluating whether a temporal correlation is causal is judgment (the model decides).

---

## Signal Sources

SitRep consumes signals from multiple source types:

- **OTel metrics and traces** via OTel Collector (Prometheus remote write, OTLP)
- **Alerts** from Alertmanager (webhook)
- **Change events** from all sources, normalised via the OpenSRM change event schema (GitHub, ArgoCD, LaunchDarkly, model registries, prompt management systems)
- **Quality scores** from the Arbiter (OTel metrics)
- **Deployment records** from CI/CD pipelines

---

## OpenSRM Integration

SitRep reads service topology from [OpenSRM](https://github.com/rsionnach/opensrm) manifests to understand dependency relationships when correlating signals. A quality drop in service A that depends on service B (as declared in the manifest) triggers SitRep to check service B's signals automatically. The manifest provides the dependency graph that makes topology-aware correlation possible.

---

## Self-Measurement

SitRep has its own judgment SLOs, measured through the [Arbiter's](https://github.com/rsionnach/arbiter) governance framework:

- **Correlation accuracy:** What percentage of SitRep's 'related change' assessments do humans agree with?
- **False positive rate:** How often does SitRep flag a change as incident-related when it isn't?

Every correlation assessment emits a `gen_ai.decision.*` OTel event, and human disagreements emit `gen_ai.override.*` events that feed SitRep's own quality measurement. If SitRep's correlation quality drops, the Arbiter's governance layer can reduce SitRep's confidence levels or flag it for human review.

---

## OpenSRM Ecosystem

SitRep is one component in the OpenSRM ecosystem. Each component solves a complete problem independently, and they compose when used together through shared OpenSRM manifests and OTel telemetry conventions.

```
                        ┌─────────────────────────┐
                        │     OpenSRM Manifest     │
                        │  (the shared contract)   │
                        └────────────┬────────────┘
                                     │
                    reads            │           reads
               ┌─────────────┬──────┴──────┬─────────────┐
               ▼             ▼             ▼             ▼
         ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
         │ Arbiter  │ │ NthLayer │ │>>SITREP< │ │  Mayday  │
         │          │ │          │ │          │ │          │
         │ quality  │ │ generate │ │correlate │ │ incident │
         │+govern   │ │ monitoring│ │ signals  │ │ response │
         │+cost     │ │          │ │          │ │          │
         └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
              │             │             │             │
              └─────────────┴──────┬──────┴─────────────┘
                                   ▼
                     ┌──────────────────────────┐
                     │      Verdict Store       │
                     │  (shared data substrate) │
                     │ create · resolve · link  │
                     │ accuracy · gaming-check  │
                     └────────────┬─────────────┘
                                  │ OTel side-effects
                                  ▼
                     ┌──────────────────────────┐
                     │    OTel Collector /      │
                     │   Prometheus / Grafana   │
                     └──────────────────────────┘

              Learning loop (post-incident):
              Mayday findings → manifest updates
              → NthLayer regenerates → Arbiter
              refines → SitRep improves → OpenSRM
```

**How SitRep fits in:**

- SitRep emits **correlation verdicts** for every correlation assessment, stored in the shared Verdict Store. Mayday consumes these verdicts (with confidence scores and lineage) as the starting context for incident response — no direct coupling between components.
- SitRep consumes **Arbiter quality verdicts** as events and correlates them with other signals (deployments, config changes, model version swaps) to identify what caused quality degradation
- SitRep reads **service topology from OpenSRM manifests** (via NthLayer's topology export) to understand dependency relationships when correlating
- SitRep's **correlation accuracy improves over time** as the learning loop feeds post-incident findings back into its models

Each component works alone. Someone who just needs signal correlation adopts SitRep without needing the Arbiter, NthLayer, or Mayday.

| Component | What it does | Link |
|-----------|-------------|------|
| **OpenSRM** | Specification for declaring service reliability requirements | [opensrm](https://github.com/rsionnach/opensrm) |
| **Verdict** | Data primitive for recording AI judgments and measuring correctness | [verdicts](https://github.com/rsionnach/verdicts) |
| **Arbiter** | Quality measurement and governance for AI agents | [arbiter](https://github.com/rsionnach/arbiter) |
| **NthLayer** | Generate monitoring infrastructure from manifests | [nthlayer](https://github.com/rsionnach/nthlayer) |
| **SitRep** | Situational awareness through signal correlation (this repo) | [sitrep](https://github.com/rsionnach/sitrep) |
| **Mayday** | Multi-agent incident response | [mayday](https://github.com/rsionnach/mayday) |

---

## Architecture

SitRep follows [Zero Framework Cognition](ZFC.md). The boundary is clear:

**Transport (code):** Ingesting events from the streaming layer, grouping signals by service and time window, maintaining the rolling pre-correlation index, computing temporal proximity between signals, generating the structured snapshot schema, publishing snapshots via API and SSE.

**Judgment (model):** Interpreting what correlations mean, assessing whether a temporal correlation is likely causal, generating the natural language summary, recommending actions, deciding the snapshot severity level.

---

## Status

SitRep is in the architecture phase. The design documented here reflects the target architecture, and implementation has not yet started. The pre-correlation concept has been validated in the existing OpenSRM ecosystem design (see the [Sitrep technical appendix](https://github.com/rsionnach/opensrm/blob/main/components/sitrep/sitrep-technical-appendix.md) in the OpenSRM repo).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

Apache License 2.0. See [LICENSE](LICENSE).
