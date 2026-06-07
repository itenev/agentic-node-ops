"""Tests for runbooks module."""

import os
import tempfile
import pytest

from agentic_node_ops.runbooks import (
    load_runbook,
    load_runbooks,
    match_runbook,
    Runbook,
    RunbookTrigger,
)


def test_load_runbook_valid():
    """Test load_runbook successfully parses a valid YAML file."""
    yaml_content = """
id: test_runbook_1
triggers:
  - alert_type: consensus_desync
    min_severity: critical
diagnostics:
  - id: diag_1
    cmd: "echo 'check status'"
    timeout: "5s"
suggested_actions:
  - id: action_1
    description: "Restart service"
    cmd: "systemctl restart service"
    risk: medium
    reversible: true
    requires_approval: false
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        path = f.name

    try:
        runbook = load_runbook(path)
        assert runbook.id == "test_runbook_1"
        assert len(runbook.triggers) == 1
        assert runbook.triggers[0].alert_type == "consensus_desync"
        assert runbook.triggers[0].min_severity == "critical"
        assert len(runbook.diagnostics) == 1
        assert runbook.diagnostics[0].id == "diag_1"
        assert len(runbook.suggested_actions) == 1
        assert runbook.suggested_actions[0].id == "action_1"
    finally:
        os.unlink(path)


def test_load_runbook_empty():
    """Test load_runbook raises ValueError on empty file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("")
        path = f.name

    try:
        with pytest.raises(ValueError, match="Empty or invalid runbook file"):
            load_runbook(path)
    finally:
        os.unlink(path)


def test_load_runbooks_directory():
    """Test load_runbooks loads all YAML files from a directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml1 = os.path.join(tmpdir, "runbook1.yaml")
        yaml2 = os.path.join(tmpdir, "runbook2.yaml")
        txt_file = os.path.join(tmpdir, "ignore.txt")

        with open(yaml1, "w") as f:
            f.write("id: rb1\ntriggers:\n  - alert_type: alert_a\n")
        with open(yaml2, "w") as f:
            f.write("id: rb2\ntriggers:\n  - alert_type: alert_b\n")
        with open(txt_file, "w") as f:
            f.write("not a yaml file")

        runbooks = load_runbooks(tmpdir)
        assert len(runbooks) == 2
        ids = {rb.id for rb in runbooks}
        assert ids == {"rb1", "rb2"}


def test_match_runbook_found():
    """Test match_runbook returns the correct runbook when alert_type and severity match."""
    runbooks = [
        Runbook(
            id="rb1",
            triggers=[RunbookTrigger(alert_type="alert_a", min_severity="high")],
        ),
        Runbook(
            id="rb2",
            triggers=[RunbookTrigger(alert_type="alert_b", min_severity="critical")],
        ),
    ]

    # Must pass severity="critical" to match the trigger's min_severity
    matched = match_runbook(runbooks, "alert_b", severity="critical")
    assert matched is not None
    assert matched.id == "rb2"


def test_match_runbook_not_found():
    """Test match_runbook returns None when no alert_type matches."""
    runbooks = [
        Runbook(
            id="rb1",
            triggers=[RunbookTrigger(alert_type="alert_a", min_severity="high")],
        ),
    ]

    matched = match_runbook(runbooks, "unknown_alert")
    assert matched is None


def test_match_runbook_multiple_triggers():
    """Test match_runbook works when a runbook has multiple triggers."""
    runbooks = [
        Runbook(
            id="rb1",
            triggers=[
                RunbookTrigger(alert_type="alert_a", min_severity="high"),
                RunbookTrigger(alert_type="alert_c", min_severity="low"),
            ],
        ),
    ]

    matched = match_runbook(runbooks, "alert_c")
    assert matched is not None
    assert matched.id == "rb1"


def test_load_runbook_with_privileged_actions():
    """Test load_runbook successfully parses privileged_actions with extra fields."""
    yaml_content = """
id: test_privileged
privileged_actions:
  - id: wipe_state
    description: "Wipe state"
    cmd: "rm -rf /data"
    risk: high
    reversible: false
    requires_approval: true
    requires_explicit_unlock: true
    phase: "4_and_above_only"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        path = f.name

    try:
        runbook = load_runbook(path)
        assert runbook.id == "test_privileged"
        assert len(runbook.privileged_actions) == 1
        action = runbook.privileged_actions[0]
        assert action.id == "wipe_state"
        assert action.requires_explicit_unlock is True
        assert action.phase == "4_and_above_only"
    finally:
        os.unlink(path)


def test_match_runbook_severity_filtering():
    """Test match_runbook correctly filters by min_severity."""
    runbooks = [
        Runbook(
            id="rb_high_only",
            triggers=[RunbookTrigger(alert_type="alert_a", min_severity="high")],
        ),
    ]

    # Should match when severity is high or critical
    assert match_runbook(runbooks, "alert_a", severity="high") is not None
    assert match_runbook(runbooks, "alert_a", severity="critical") is not None

    # Should NOT match when severity is low or medium
    assert match_runbook(runbooks, "alert_a", severity="low") is None
    assert match_runbook(runbooks, "alert_a", severity="medium") is None
