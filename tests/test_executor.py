"""Tests for runbook executor."""

import time
from agentic_node_ops.executor import execute_command, run_diagnostics, execute_action, _parse_timeout
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
    result = execute_command("false")
    assert result["success"] is False
    assert result["returncode"] == 1


def test_execute_command_timeout_kills_process():
    """Test command timeout kills the entire process group."""
    # Use a shell command that spawns a child (sleep)
    # If process group kill works, the sleep process should not outlive the call
    start = time.time()
    result = execute_command("sleep 5", timeout=1)
    elapsed = time.time() - start
    
    assert result["success"] is False
    assert "timed out" in result["stderr"].lower()
    assert result["returncode"] == -1
    # Should return quickly (around 1s), not wait for the 5s sleep to finish
    assert elapsed < 2.0


def test_execute_command_shell_required():
    """Test shell_required allows shell metacharacters."""
    result = execute_command("echo 'hello' && echo 'world'", shell_required=True)
    assert result["success"] is True
    assert "hello" in result["stdout"]
    assert "world" in result["stdout"]


def test_execute_command_shell_false_blocks_metacharacters():
    """Test shell=False treats metacharacters as literal arguments."""
    # This will fail because '&&' is passed as a literal argument, not as a shell operator
    result2 = execute_command("ls /nonexistent_dir_12345 && echo 'should not run'", shell_required=False)
    assert result2["success"] is False
    assert "No such file or directory" in result2["stderr"]


def test_run_diagnostics():
    """Test running diagnostics from a runbook."""
    runbook = Runbook(
        id="test_runbook",
        diagnostics=[
            RunbookDiagnostic(id="diag1", cmd="echo 'diag1'", timeout="1s", description="Test diag"),
            RunbookDiagnostic(id="diag2", cmd="exit 1", timeout="1s"),
        ]
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


def test_parse_timeout_valid():
    """Test _parse_timeout with valid inputs."""
    assert _parse_timeout("5s") == 5
    assert _parse_timeout("1m") == 60
    assert _parse_timeout("30") == 30
    assert _parse_timeout(" 2m ") == 120


def test_parse_timeout_invalid():
    """Test _parse_timeout with invalid inputs defaults to 30s."""
    assert _parse_timeout("abc") == 30
    assert _parse_timeout("") == 30
    assert _parse_timeout("5x") == 30
