"""Tests for context_fetcher module."""

import json
from unittest.mock import patch, MagicMock


from webhook_receiver.context_fetcher import (
    _query_prometheus,
    _get_docker_container_status,
    fetch_context_snapshot,
)


class TestQueryPrometheus:
    @patch("webhook_receiver.context_fetcher.urllib.request.urlopen")
    def test_query_prometheus_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"data": {"result": [{"value": [1704067200, "42.5"]}]}}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = _query_prometheus("lighthouse_peers")
        assert result == 42.5

    @patch("webhook_receiver.context_fetcher.urllib.request.urlopen")
    def test_query_prometheus_failure(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = _query_prometheus("lighthouse_peers")
        assert result is None

    @patch("webhook_receiver.context_fetcher.urllib.request.urlopen")
    def test_query_prometheus_empty_result(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": {"result": []}}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = _query_prometheus("lighthouse_peers")
        assert result is None


class TestGetDockerContainerStatus:
    @patch("webhook_receiver.context_fetcher.socket.socket")
    def test_docker_status_running(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"State": {"Status": "running"}}'
        )
        mock_sock.recv.side_effect = [response, b""]

        status, note = _get_docker_container_status("consensus")
        assert status == "running"
        assert note is None

        # Verify Connection: close header is sent to prevent hanging
        sent_request = mock_sock.sendall.call_args[0][0].decode("utf-8")
        assert "Connection: close\r\n" in sent_request

    @patch("webhook_receiver.context_fetcher.socket.socket")
    def test_docker_status_exited(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"State": {"Status": "exited"}}'
        )
        mock_sock.recv.side_effect = [response, b""]

        status, note = _get_docker_container_status("consensus")
        assert status == "exited"
        assert "exited" in note

    @patch("webhook_receiver.context_fetcher.socket.socket")
    def test_docker_status_connection_refused(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        status, note = _get_docker_container_status("consensus")
        assert status is None
        assert "ConnectionRefusedError" in note


class TestFetchContextSnapshot:
    @patch("webhook_receiver.context_fetcher._get_docker_container_status")
    @patch("webhook_receiver.context_fetcher._query_prometheus")
    def test_fetch_success_no_fallback(self, mock_prom, mock_docker):
        mock_docker.return_value = ("running", None)
        mock_prom.return_value = 50.0

        snapshot = fetch_context_snapshot("host1", "consensus", "lighthouse")

        assert snapshot.container_status == "running"
        assert snapshot.container_status_note is None
        assert snapshot.peer_count == 50
        assert snapshot.prometheus_fallback_used is False

    @patch("webhook_receiver.context_fetcher._get_docker_container_status")
    @patch("webhook_receiver.context_fetcher._query_prometheus")
    def test_fetch_docker_fallback_to_prometheus(self, mock_prom, mock_docker):
        mock_docker.return_value = (None, "docker socket error")
        # Calls: 1. lighthouse_peers, 2. lighthouse_validator_count
        mock_prom.side_effect = [50.0, 3.0]

        snapshot = fetch_context_snapshot("host1", "consensus", "lighthouse")

        assert snapshot.container_status == "unavailable"
        assert snapshot.container_status_note == "docker socket unreachable"
        assert snapshot.peer_count == 50
        assert snapshot.validator_count == 3
        assert snapshot.prometheus_fallback_used is True

    @patch("webhook_receiver.context_fetcher._get_docker_container_status")
    @patch("webhook_receiver.context_fetcher._query_prometheus")
    def test_fetch_all_unavailable(self, mock_prom, mock_docker):
        mock_docker.return_value = (None, "error")
        mock_prom.return_value = None

        snapshot = fetch_context_snapshot("host1", "consensus", "lighthouse")

        assert snapshot.container_status == "unavailable"
        assert snapshot.peer_count is None
        assert snapshot.prometheus_fallback_used is True

    @patch("webhook_receiver.context_fetcher._get_docker_container_status")
    @patch("webhook_receiver.context_fetcher._query_prometheus")
    def test_fetch_no_container(self, mock_prom, mock_docker):
        mock_prom.return_value = 50.0

        snapshot = fetch_context_snapshot("host1", container=None, client="lighthouse")

        assert snapshot.container_status is None
        assert snapshot.peer_count == 50
        assert snapshot.prometheus_fallback_used is False
