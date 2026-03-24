"""SitRep CLI — serve, status, replay."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
import yaml

from nthlayer_correlate.config import SitRepConfig, load_config
from nthlayer_correlate.correlation.changes import find_change_candidates
from nthlayer_correlate.correlation.dedup import deduplicate
from nthlayer_correlate.correlation.engine import CorrelationEngine
from nthlayer_correlate.correlation.temporal import group_temporal
from nthlayer_correlate.correlation.topology import group_topology
from nthlayer_correlate.ingestion.severity import pre_score
from nthlayer_correlate.snapshot.generator import SnapshotBudget, SnapshotGenerator
from nthlayer_correlate.snapshot.model import ModelInterface
from nthlayer_correlate.state import StateMachine
from nthlayer_correlate.store.sqlite import SQLiteEventStore
from nthlayer_correlate.types import AgentState, CorrelationGroup, EventType, SitRepEvent

logger = structlog.get_logger()

REFERENCE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def parse_relative_time(at_str: str) -> str:
    """Parse 'T+Nm' to ISO 8601."""
    match = re.match(r"T\+(\d+)m", at_str)
    if not match:
        raise ValueError(f"Invalid time format: {at_str}")
    minutes = int(match.group(1))
    ts = REFERENCE_TIME + timedelta(minutes=minutes)
    return ts.isoformat()


def scenario_event_to_sitrep(evt_data: dict, index: int) -> SitRepEvent:
    """Convert a scenario event dict to a SitRepEvent."""
    ts = parse_relative_time(evt_data["at"])
    payload = evt_data["payload"]
    service = payload.get("service", "unknown")
    return SitRepEvent(
        id=f"scenario-evt-{index:04d}",
        timestamp=ts,
        source="scenario",
        type=EventType(evt_data["type"]),
        service=service,
        environment="production",
        severity=0.5,
        payload=payload,
    )


def _build_topology_dict(scenario: dict) -> dict[str, Any] | None:
    """Build the topology dict expected by the correlation engine from scenario YAML."""
    topo_section = scenario.get("topology")
    if not topo_section:
        return None
    services = topo_section.get("services", [])
    if not services:
        return None
    result: dict[str, Any] = {}
    for svc in services:
        name = svc["name"]
        result[name] = {
            "tier": svc.get("tier", "standard"),
            "dependencies": svc.get("dependencies", []),
            "dependents": svc.get("dependents", []),
        }
    return result


def replay_command(
    scenario_path: str,
    config_path: str | None,
    no_model: bool,
    store_dir: str,
) -> int:
    """Replay a scenario fixture. Returns exit code."""
    # Load scenario
    try:
        with open(scenario_path) as f:
            raw = yaml.safe_load(f)
    except (FileNotFoundError, OSError) as exc:
        logger.error("scenario_load_failed", path=scenario_path, error=str(exc))
        return 2

    scenario = raw.get("scenario", raw)

    # Parse events
    events_data = scenario.get("events", [])
    events: list[SitRepEvent] = []
    for i, evt_data in enumerate(events_data):
        try:
            event = scenario_event_to_sitrep(evt_data, i)
            events.append(event)
        except (ValueError, KeyError) as exc:
            logger.warning("event_parse_failed", index=i, error=str(exc))

    # Build topology
    topology = _build_topology_dict(scenario)

    # Open temp store
    db_path = os.path.join(store_dir, "replay.db")
    store = SQLiteEventStore(db_path)

    try:
        # Insert events
        if events:
            store.insert_batch(events)

        # Compute the actual time window from events for correlation
        if events:
            timestamps = [e.timestamp for e in events]
            start_ts = min(timestamps)
            end_ts = max(timestamps)
            # Add a buffer to ensure the window captures all events
            end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
            end_buffered = (end_dt + timedelta(minutes=1)).isoformat()

            # Run correlation sub-steps manually (since engine uses datetime.now())
            all_events = store.get_by_time_window(start_ts, end_buffered)

            if all_events:
                # Deduplicate
                deduped = deduplicate(all_events)

                # Severity enrichment
                enriched = []
                for event in deduped:
                    new_severity = pre_score(event, None)
                    if new_severity != event.severity:
                        event = replace(event, severity=new_severity)
                    enriched.append(event)

                # Compute window minutes from the event spread
                start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                window_minutes = max(
                    int((end_dt - start_dt).total_seconds() / 60) + 1,
                    5,
                )

                # Temporal grouping
                temporal_groups = group_temporal(enriched, window_minutes=window_minutes)

                # Topology grouping
                topology_correlations = group_topology(temporal_groups, topology)

                # Change candidate indexing
                # Since get_recent_changes uses datetime('now'), we query the store
                # directly for change events in our scenario window
                change_candidates_map = find_change_candidates(
                    store, temporal_groups, topology=topology,
                    window_minutes=window_minutes + 30,
                )

                # Assemble correlation groups using the engine helper
                engine = CorrelationEngine()
                groups = _assemble_groups(
                    engine, temporal_groups, topology_correlations,
                    change_candidates_map, topology,
                )
            else:
                groups = []
        else:
            groups = []

        # Report
        scenario_id = scenario.get("id", "unknown")
        print(f"\n=== Replay: {scenario_id} ===")
        print(f"Events inserted: {len(events)}")
        print(f"Correlation groups found: {len(groups)}")

        services_affected: set[str] = set()
        total_changes = 0
        for g in groups:
            services_affected.update(g.services)
            total_changes += len(g.change_candidates)
            print(f"  [{g.id}] P{g.priority}: {g.summary}")

        print(f"Services affected: {sorted(services_affected)}")
        print(f"Change candidates: {total_changes}")

        if not no_model:
            # Model-enabled path
            config = load_config(config_path) if config_path else SitRepConfig()
            generator = SnapshotGenerator(SnapshotBudget(config.token_budget))
            model = ModelInterface(config.model_name, config.model_max_tokens)

            if groups:
                prompt, cache_hit = generator.generate(groups, AgentState.WATCHING)
                try:
                    verdicts = asyncio.run(model.interpret(prompt, groups))
                    print(f"Verdicts created: {len(verdicts)}")
                except Exception as exc:
                    logger.warning("model_call_failed", error=str(exc))
                    print("Model call failed, skipping verdicts")
        else:
            print("Model: skipped (--no-model)")

        print()
        return 0

    finally:
        store.close()


def _assemble_groups(
    engine: CorrelationEngine,
    temporal_groups: list,
    topology_correlations: list,
    change_candidates_map: dict,
    topology: dict | None,
) -> list[CorrelationGroup]:
    """Assemble CorrelationGroups from correlation sub-step outputs.

    Mirrors the assembly logic in CorrelationEngine.correlate().
    """
    import uuid
    from nthlayer_correlate.types import ChangeCandidate, TemporalGroup

    assigned: set[int] = set()
    correlation_groups: list[CorrelationGroup] = []

    # First pass: topology-linked groups
    for tc in topology_correlations:
        involved_services = {tc.primary_service}
        for rs in tc.related_services:
            involved_services.add(rs["service"])

        signals: list[TemporalGroup] = []
        for gi, tg in enumerate(temporal_groups):
            if tg.service in involved_services and gi not in assigned:
                signals.append(tg)
                assigned.add(gi)

        if not signals:
            continue

        all_changes: list[ChangeCandidate] = []
        for svc in involved_services:
            all_changes.extend(change_candidates_map.get(svc, []))

        peak_severity = max(s.peak_severity for s in signals)
        priority = engine._compute_priority(
            peak_severity, list(involved_services), topology, topology_correlations
        )

        total_events = sum(s.count for s in signals)
        primary_type = engine._dominant_event_type(signals)
        services_str = ", ".join(sorted(involved_services))
        summary = (
            f"{total_events} {primary_type}(s) on {services_str} "
            f"with {len(all_changes)} recent change(s)"
        )

        all_timestamps = []
        for s in signals:
            all_timestamps.append(s.time_window[0])
            all_timestamps.append(s.time_window[1])
        first_seen = min(all_timestamps)
        last_updated = max(all_timestamps)

        group_id = f"cg-{uuid.uuid4().hex[:8]}"
        correlation_groups.append(
            CorrelationGroup(
                id=group_id,
                priority=priority,
                summary=summary,
                services=sorted(involved_services),
                signals=signals,
                topology=tc,
                change_candidates=all_changes,
                first_seen=first_seen,
                last_updated=last_updated,
                event_count=total_events,
            )
        )

    # Second pass: unassigned temporal groups
    for gi, tg in enumerate(temporal_groups):
        if gi in assigned:
            continue

        service = tg.service
        changes = change_candidates_map.get(service, [])
        peak_severity = tg.peak_severity
        priority = engine._compute_priority(
            peak_severity, [service], topology, topology_correlations
        )

        primary_type = engine._dominant_event_type([tg])
        summary = (
            f"{tg.count} {primary_type}(s) on {service} "
            f"with {len(changes)} recent change(s)"
        )

        group_id = f"cg-{uuid.uuid4().hex[:8]}"
        correlation_groups.append(
            CorrelationGroup(
                id=group_id,
                priority=priority,
                summary=summary,
                services=[service],
                signals=[tg],
                topology=None,
                change_candidates=changes,
                first_seen=tg.time_window[0],
                last_updated=tg.time_window[1],
                event_count=tg.count,
            )
        )

    correlation_groups.sort(
        key=lambda g: (g.priority, -max(s.peak_severity for s in g.signals))
    )

    return correlation_groups


def status_command(config_path: str | None, store_dir: str | None = None) -> int:
    """Show current SitRep status. Returns exit code."""
    config = load_config(config_path) if config_path else SitRepConfig()

    # Use store_dir if provided (for testing), otherwise use config path
    if store_dir:
        db_path = os.path.join(store_dir, "sitrep-events.db")
    else:
        db_path = config.store_path

    store = SQLiteEventStore(db_path)
    try:
        stats = store.get_stats()

        print("\n=== SitRep Status ===")
        print(f"Agent state: {AgentState.WATCHING.value}")
        print(f"Event count: {stats['event_count']}")
        print(f"Oldest event: {stats['min_timestamp'] or 'none'}")
        print(f"Newest event: {stats['max_timestamp'] or 'none'}")

        # DB file size
        if os.path.exists(db_path):
            size_bytes = os.path.getsize(db_path)
            if size_bytes < 1024:
                size_str = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
            print(f"DB size: {size_str}")
        else:
            print("DB size: 0 B")

        print()
        return 0

    finally:
        store.close()


async def _serve_loop(config: SitRepConfig) -> None:
    """Run the full serve pipeline."""
    from nthlayer_correlate.ingestion.webhook import WebhookIngester

    store = SQLiteEventStore(config.store_path)
    ingester = WebhookIngester(config.ingestion_host, config.ingestion_port)
    engine = CorrelationEngine()
    generator = SnapshotGenerator(SnapshotBudget(config.token_budget))
    model = ModelInterface(config.model_name, config.model_max_tokens)
    state_machine = StateMachine()

    # Buffer events via queue to avoid concurrent SQLite writes
    event_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
    ingester.on_event(lambda event: event_queue.put_nowait(event))

    # Start ingester
    await ingester.start()
    logger.info(
        "sitrep_started",
        host=config.ingestion_host,
        port=config.ingestion_port,
    )

    # Shutdown event
    shutdown = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("shutdown_requested")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    # Optionally open verdict store
    verdict_store = None
    try:
        from nthlayer_learn.store import VerdictStore
        verdict_store = VerdictStore(config.verdict_store_path)
    except Exception:
        logger.info("verdict_store_not_available")

    try:
        while not shutdown.is_set():
            interval = state_machine.get_interval()
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal cycle

            # Drain buffered events into store (single-threaded, safe)
            while not event_queue.empty():
                try:
                    event = event_queue.get_nowait()
                    store.insert(event)
                except asyncio.QueueEmpty:
                    break

            # Run correlation
            try:
                groups = engine.correlate(
                    store,
                    config.correlation_window_minutes,
                    topology=None,  # No manifests in Tier 1
                )

                model_healthy = True
                state_machine.update(groups, model_healthy)

                prompt, cache_hit = generator.generate(groups, state_machine.state)

                if not cache_hit and groups:
                    try:
                        await model.interpret(prompt, groups, verdict_store)
                    except Exception as exc:
                        logger.warning("model_call_failed", error=str(exc))
                        state_machine.update(groups, model_healthy=False)

                logger.info(
                    "correlation_cycle",
                    state=state_machine.state.value,
                    groups=len(groups),
                    cache_hit=cache_hit,
                )

            except Exception as exc:
                logger.error("correlation_error", error=str(exc))

    finally:
        await ingester.stop()
        store.close()
        logger.info("sitrep_stopped")


def serve_command(config_path: str | None) -> int:
    """Start the full SitRep pipeline. Returns exit code."""
    config = load_config(config_path) if config_path else SitRepConfig()
    try:
        asyncio.run(_serve_loop(config))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("serve_failed", error=str(exc))
        return 2


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="nthlayer-correlate",
        description="SitRep — Situational awareness through automated signal correlation",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the full SitRep pipeline")
    serve_parser.add_argument(
        "--config", default=None, help="Path to sitrep.yaml config file"
    )

    # status
    status_parser = subparsers.add_parser("status", help="Show current SitRep status")
    status_parser.add_argument(
        "--config", default=None, help="Path to sitrep.yaml config file"
    )

    # replay
    replay_parser = subparsers.add_parser("replay", help="Replay a scenario fixture")
    replay_parser.add_argument(
        "--scenario", required=True, help="Path to scenario YAML file"
    )
    replay_parser.add_argument(
        "--config", default=None, help="Path to sitrep.yaml config file"
    )
    replay_parser.add_argument(
        "--no-model", action="store_true", help="Skip model calls"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(2)

    if args.command == "serve":
        sys.exit(serve_command(args.config))
    elif args.command == "status":
        sys.exit(status_command(args.config))
    elif args.command == "replay":
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys.exit(
                replay_command(
                    scenario_path=args.scenario,
                    config_path=args.config,
                    no_model=args.no_model,
                    store_dir=tmp_dir,
                )
            )


if __name__ == "__main__":
    main()
