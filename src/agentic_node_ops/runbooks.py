"""Runbook loading and matching utilities."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class RunbookTrigger:
    alert_type: str
    min_severity: str = "low"


@dataclass
class RunbookAction:
    id: str
    description: str
    cmd: str
    risk: str
    reversible: bool
    requires_approval: bool
    approval_timeout: str = "30m"
    pre_conditions: list[str] = field(default_factory=list)
    requires_explicit_unlock: bool = False
    phase: str | None = None


@dataclass
class RunbookDiagnostic:
    id: str
    cmd: str
    timeout: str = "5s"


@dataclass
class Runbook:
    id: str
    triggers: list[RunbookTrigger] = field(default_factory=list)
    diagnostics: list[RunbookDiagnostic] = field(default_factory=list)
    suggested_actions: list[RunbookAction] = field(default_factory=list)
    privileged_actions: list[RunbookAction] = field(default_factory=list)


def load_runbook(file_path: str | Path) -> Runbook:
    """Load a single runbook from a YAML file."""
    with open(file_path, "r") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"Empty or invalid runbook file: {file_path}")

    triggers = [RunbookTrigger(**t) for t in data.get("triggers", [])]
    diagnostics = [RunbookDiagnostic(**d) for d in data.get("diagnostics", [])]
    suggested_actions = [RunbookAction(**a) for a in data.get("suggested_actions", [])]
    privileged_actions = [
        RunbookAction(**a) for a in data.get("privileged_actions", [])
    ]

    return Runbook(
        id=data.get("id", ""),
        triggers=triggers,
        diagnostics=diagnostics,
        suggested_actions=suggested_actions,
        privileged_actions=privileged_actions,
    )


def load_runbooks(directory: str | Path) -> list[Runbook]:
    """Load all runbooks from a directory."""
    runbooks = []
    dir_path = Path(directory)
    for file_path in dir_path.glob("*.yaml"):
        try:
            runbooks.append(load_runbook(file_path))
        except Exception as e:
            log.warning("Failed to load runbook %s: %s", file_path, e)
    return runbooks


SEVERITY_LEVELS = ["low", "medium", "high", "critical"]


def match_runbook(
    runbooks: list[Runbook], alert_type: str, severity: str = "low"
) -> Optional[Runbook]:
    """Return the matching runbook based on alert_type and severity, or None if not found."""
    alert_severity_idx = (
        SEVERITY_LEVELS.index(severity) if severity in SEVERITY_LEVELS else 0
    )

    for runbook in runbooks:
        for trigger in runbook.triggers:
            if trigger.alert_type == alert_type:
                min_sev_idx = (
                    SEVERITY_LEVELS.index(trigger.min_severity)
                    if trigger.min_severity in SEVERITY_LEVELS
                    else 0
                )
                if alert_severity_idx >= min_sev_idx:
                    return runbook
    return None
