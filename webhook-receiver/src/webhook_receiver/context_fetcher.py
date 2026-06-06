"""Context snapshot fetcher for webhook receiver.

Pre-fetches cheap context (peer count, sync status, container status)
at receive time with Prometheus fallback.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional, Tuple

from .types import ContextSnapshot

log = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
DOCKER_SOCKET_PATH = os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock")


def _query_prometheus(query: str) -> Optional[float]:
    """Query Prometheus for a single instant vector value."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            result = data.get("data", {}).get("result", [])
            if result and len(result) > 0:
                return float(result[0]["value"][1])
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        TypeError,
    ) as e:
        log.debug("Prometheus query failed for %s: %s", query, e)
    return None


def _get_docker_container_status(container_name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Query Docker socket for container status.
    Returns (status, note).
    """
    api_path = f"/containers/{container_name}/json"
    request = f"GET {api_path} HTTP/1.1\r\nHost: docker\r\n\r\n"
    
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(DOCKER_SOCKET_PATH)
            sock.sendall(request.encode('utf-8'))
            
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                
            header_end = response.find(b"\r\n\r\n")
            if header_end == -1:
                return None, "malformed response"
            
            body = response[header_end + 4:]
            data = json.loads(body.decode('utf-8'))
            state = data.get("State", {})
            status = state.get("Status", "unknown")
            
            if status == "running":
                return "running", None
            else:
                return status, f"container is {status}"
    except (socket.error, json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        log.debug("Docker socket query failed for %s: %s", container_name, e)
        return None, f"docker socket error: {type(e).__name__}"


def fetch_context_snapshot(
    host: str,
    container: Optional[str] = None,
    client: Optional[str] = None,
) -> ContextSnapshot:
    """
    Fetch context snapshot for an alert.
    
    Fallback chain:
    1. Primary source (Docker socket for container status, direct metrics)
    2. Prometheus last value
    3. "unavailable"
    """
    snapshot = ContextSnapshot()
    fallback_used = False
    
    # 1. Container status
    if container:
        status, note = _get_docker_container_status(container)
        if status:
            snapshot.container_status = status
            if note:
                snapshot.container_status_note = note
        else:
            # Fallback to Prometheus: check if container is up
            prom_up = _query_prometheus(f'up{{container="{container}"}}')
            if prom_up == 1.0:
                snapshot.container_status = "running"
                snapshot.container_status_note = "docker socket unreachable, using prometheus fallback"
                fallback_used = True
            else:
                snapshot.container_status = "unavailable"
                snapshot.container_status_note = "docker socket and prometheus unreachable"
                fallback_used = True

    # 2. Peer count
    peer_queries = []
    if client == "lighthouse":
        peer_queries = ["lighthouse_peers", "beacon_peers"]
    elif client in ("prysm", "teku", "nimbus", "lodestar"):
        peer_queries = ["beacon_peers", "p2p_peers"]
    
    for q in peer_queries:
        peers = _query_prometheus(q)
        if peers is not None:
            snapshot.peer_count = int(peers)
            break
    else:
        if peer_queries:
            fallback_used = True

    # 3. Validator count
    val_queries = []
    if client == "lighthouse":
        val_queries = ["lighthouse_validator_count", "validator_count"]
    elif client in ("prysm", "teku"):
        val_queries = ["beacon_validators_total", "validator_count"]
    
    for q in val_queries:
        val_count = _query_prometheus(q)
        if val_count is not None:
            snapshot.validator_count = int(val_count)
            break
    else:
        if val_queries:
            fallback_used = True

    snapshot.prometheus_fallback_used = fallback_used
    return snapshot
