"""Tests for SitRep CLI."""
from __future__ import annotations

import os

import pytest

from nthlayer_correlate.cli import parse_relative_time, replay_command, status_command


class TestParseRelativeTime:
    def test_zero_minutes(self):
        result = parse_relative_time("T+0m")
        assert "2026-01-01T00:00:00" in result

    def test_twelve_minutes(self):
        result = parse_relative_time("T+12m")
        assert "2026-01-01T00:12:00" in result

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_relative_time("invalid")


class TestReplayCommand:
    def test_replay_cascading_failure_no_model(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "cascading-failure.yaml"
        )
        result = replay_command(
            scenario_path=scenario_path,
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 0  # success

    def test_replay_quiet_period_no_model(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "quiet-period.yaml"
        )
        result = replay_command(
            scenario_path=scenario_path,
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 0

    def test_replay_nonexistent_scenario(self, tmp_path):
        result = replay_command(
            scenario_path="/nonexistent/path.yaml",
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 2  # error

    def test_replay_misleading_correlation_no_model(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "misleading-correlation.yaml"
        )
        result = replay_command(
            scenario_path=scenario_path,
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 0


class TestStatusCommand:
    def test_status_default(self, tmp_path):
        result = status_command(config_path=None, store_dir=str(tmp_path))
        assert result == 0
