"""Runbook executor for agentic-node-ops.

Executes diagnostics and actions defined in runbooks.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
from typing import Any

from .runbooks import Runbook, RunbookAction

log = logging.getLogger(__name__)


def _parse_timeout(timeout_str: str) -> int:
    """Parse a timeout string (e.g., '5s', '1m') into seconds."""
    try:
        timeout_str = timeout_str.strip().lower()
        if timeout_str.endswith("m"):
            return int(timeout_str[:-1]) * 60
        if timeout_str.endswith("s"):
            return int(timeout_str[:-1])
        return int(timeout_str)
    except (ValueError, AttributeError):
        log.warning("Invalid timeout string '%s', defaulting to 30s", timeout_str)
        return 30


def execute_command(
    cmd: str, timeout: int = 30, shell_required: bool = False
) -> dict[str, Any]:
    """Execute a shell command and return the result.

    Returns a dict with 'success', 'stdout', 'stderr', and 'returncode'.
    Uses shell=False with shlex.split by default to prevent shell injection.
    If shell_required is True, uses shell=True and ensures the entire process
    group is killed on timeout to prevent orphaned child processes.
    """
    try:
        if shell_required:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        else:
            proc = subprocess.Popen(
                shlex.split(cmd),
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.strip() if stdout else "",
                "stderr": stderr.strip() if stderr else "",
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            # Kill the entire process group to prevent orphaned children
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # Process already terminated
            proc.wait()
            log.error("Command timed out after %ds: %s", timeout, cmd)
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "returncode": -1,
            }
    except Exception as e:
        log.error("Command execution failed: %s, error: %s", cmd, e)
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        }


def run_diagnostics(runbook: Runbook) -> list[dict[str, Any]]:
    """Run all diagnostics for a given runbook.

    Diagnostics are TIER 1 actions: always run, no approval, no notification.
    """
    results = []
    for diag in runbook.diagnostics:
        timeout_sec = _parse_timeout(diag.timeout)
        res = execute_command(diag.cmd, timeout=timeout_sec)
        results.append(
            {
                "id": diag.id,
                "cmd": diag.cmd,
                "description": diag.description,
                **res,
            }
        )
    return results


def execute_action(action: RunbookAction) -> dict[str, Any]:
    """Execute a runbook action.

    TIER 2 (suggested_actions) and TIER 3 (privileged_actions) are handled here.
    Privileged actions requiring explicit unlock will be blocked.
    """
    if action.requires_explicit_unlock:
        log.warning(
            "Blocked execution of privileged action '%s': requires explicit unlock",
            action.id,
        )
        return {
            "id": action.id,
            "cmd": action.cmd,
            "success": False,
            "stdout": "",
            "stderr": "Action requires explicit unlock (privileged action blocked)",
            "returncode": -1,
        }

    # Default execution timeout of 60 seconds for actions
    timeout_sec = 60

    res = execute_command(
        action.cmd, timeout=timeout_sec, shell_required=action.shell_required
    )
    return {
        "id": action.id,
        "cmd": action.cmd,
        "description": action.description,
        **res,
    }
