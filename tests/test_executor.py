"""Tests for runbook executor."""

from agentic_node_ops.executor import execute_command, run_diagnostics, execute_action
from agentic_node_ops.runbooks import Runbook, RunbookDiagnostic, RunbookAction


def test_execute_command_success():
    """Test successful command execution."""
    result = execute_command("echo 'hello'")
    assert result["success"] is True
    assert result["stdout"] == "hello"
    assert result["stderr"] == ""
    assert result["returncode"] == 0


def test_execute_command_failure():
    """Test failed command execution."""
    result = execute_command("exit 1")
    assert result["success"] is False
    assert result["returncode"] == 1


def test_execute_command_timeout():
    """Test command timeout."""
    result = execute_command("sleep 2", timeout=1)
    assert result["success"] is False
    assert "timed out" in result["stderr"].lower()
    assert result["returncode"] == -1


def test_run_diagnostics():
    """Test running diagnostics from a runbook."""
    runbook = Runbook(
        id="test_runbook",
        diagnostics=[
            RunbookDiagnostic(
                id="diag1", cmd="echo 'diag1'", timeout="1s", description="Test diag"
            ),
            RunbookDiagnostic(id="diag2", cmd="exit 1", timeout="1s"),
        ],
    )
    results = run_diagnostics(runbook)

    assert len(results) == 2
    assert results[0]["id"] == "diag1"
    assert results[0]["success"] is True
    assert results[0]["stdout"] == "diag1"

    assert results[1]["id"] == "diag2"
    assert results[1]["success"] is False


def test_execute_action_success():
    """Test successful action execution."""
    action = RunbookAction(
        id="action1",
        description="Test action",
        cmd="echo 'action1'",
        risk="low",
        reversible=True,
        requires_approval=True,
    )
    result = execute_action(action)

    assert result["success"] is True
    assert result["stdout"] == "action1"
    assert result["id"] == "action1"


def test_execute_action_privileged_blocked():
    """Test that privileged actions requiring unlock are blocked."""
    action = RunbookAction(
        id="privileged_action",
        description="Dangerous action",
        cmd="echo 'should not run'",
        risk="high",
        reversible=False,
        requires_approval=True,
        requires_explicit_unlock=True,
    )
    result = execute_action(action)

    assert result["success"] is False
    assert "requires explicit unlock" in result["stderr"]
    assert result["returncode"] == -1
