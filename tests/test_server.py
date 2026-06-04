from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from cowp.config import load_project_config, write_json
from cowp.server import (
    ServerError,
    dashboard_html,
    dashboard_url,
    make_backlog_server,
    validate_serve_options,
)


def test_server_routes_return_dashboard_health_and_snapshot(git_repo: Path, workerpool_config: Path):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "served feature",
            "status": "draft",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [],
        },
    )
    config = load_project_config(git_repo)
    port = _free_port()
    server = make_backlog_server(config, host="127.0.0.1", port=port, refresh_ms=1000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        html_response = _get(f"http://127.0.0.1:{port}/")
        assert html_response["status"] == 200
        assert "text/html" in html_response["content_type"]
        assert html_response["cache_control"] == "no-store"
        assert "WorkerPool Backlog" in html_response["body"]
        assert "/api/backlog.json" in html_response["body"]

        health_response = _get(f"http://127.0.0.1:{port}/api/health")
        assert json.loads(health_response["body"]) == {"ok": True}
        assert "application/json" in health_response["content_type"]
        assert health_response["cache_control"] == "no-store"

        snapshot_response = _get(f"http://127.0.0.1:{port}/api/backlog.json")
        snapshot = json.loads(snapshot_response["body"])
        assert snapshot["repo"] == str(git_repo.resolve())
        assert snapshot["columns"]
        draft = next(column for column in snapshot["columns"] if column["title"] == "Draft")
        assert draft["features"][0]["feature_id"] == "FEATURE-001"

        try:
            urlopen(f"http://127.0.0.1:{port}/missing", timeout=5)
        except HTTPError as exc:
            assert exc.code == 404
            assert exc.headers["Cache-Control"] == "no-store"
        else:
            raise AssertionError("expected 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_backlog_json_reads_fresh_state_on_each_request(git_repo: Path, workerpool_config: Path):
    _write_feature_plan(
        git_repo,
        "FEATURE-001",
        {
            "feature_id": "FEATURE-001",
            "title": "refresh state",
            "status": "exported",
            "depends_on_features": [],
            "markdown": "plans/FEATURE-001.md",
            "open_decisions": [],
            "review_findings": [],
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "task",
                    "status": "exported",
                    "allowed_files": ["src/example.py"],
                    "prompt": "WRITE src/example.py",
                }
            ],
        },
    )
    config = load_project_config(git_repo)
    port = _free_port()
    server = make_backlog_server(config, host="127.0.0.1", port=port, refresh_ms=1000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first = json.loads(_get(f"http://127.0.0.1:{port}/api/backlog.json")["body"])
        assert _feature_column(first, "FEATURE-001") == "Exported"

        from cowp.state import StateStore

        StateStore(config.runs_root).update("TASK-001", status="running")
        second = json.loads(_get(f"http://127.0.0.1:{port}/api/backlog.json")["body"])
        assert _feature_column(second, "FEATURE-001") == "Running"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_option_validation():
    validate_serve_options(host="127.0.0.1", port=8765, refresh_ms=1000)
    validate_serve_options(host="localhost", port=8765, refresh_ms=1000)
    validate_serve_options(host="::1", port=8765, refresh_ms=1000)
    assert dashboard_url("::1", 8765) == "http://[::1]:8765"

    for kwargs in [
        {"host": "0.0.0.0", "port": 8765, "refresh_ms": 1000},
        {"host": "127.0.0.1", "port": 0, "refresh_ms": 1000},
        {"host": "127.0.0.1", "port": 8765, "refresh_ms": 999},
    ]:
        try:
            validate_serve_options(**kwargs)
        except ServerError:
            pass
        else:
            raise AssertionError(f"expected ServerError for {kwargs}")


def test_dashboard_html_uses_boot_script_without_embedding_user_data():
    html = dashboard_html(3000)

    assert "setInterval(refresh, refreshMs)" in html
    assert "textContent" in html
    assert "innerHTML" not in html


def _get(url: str) -> dict[str, str | int]:
    with urlopen(url, timeout=5) as response:
        return {
            "status": response.status,
            "content_type": response.headers["Content-Type"],
            "cache_control": response.headers["Cache-Control"],
            "body": response.read().decode("utf-8"),
        }


def _feature_column(snapshot: dict, feature_id: str) -> str:
    for column in snapshot["columns"]:
        for feature in column["features"]:
            if feature["feature_id"] == feature_id:
                return column["title"]
    raise AssertionError(f"feature not found: {feature_id}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_feature_plan(repo: Path, feature_id: str, data: dict) -> Path:
    path = repo / ".codex-workerpool" / "plans" / f"{feature_id}.plan.json"
    write_json(path, data)
    markdown = repo / ".codex-workerpool" / "plans" / f"{feature_id}.md"
    markdown.write_text(f"# {feature_id}\n", encoding="utf-8")
    return path
