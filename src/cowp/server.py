from __future__ import annotations

import json
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from cowp.backlog import backlog_snapshot_to_dict, build_backlog_snapshot
from cowp.config import ProjectConfig

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ServerError(RuntimeError):
    """Raised when the local dashboard cannot be served."""


def serve_backlog(
    config: ProjectConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh_ms: int = 3000,
    open_browser: bool = True,
) -> None:
    server = make_backlog_server(config, host=host, port=port, refresh_ms=refresh_ms)
    url = dashboard_url(host, port)
    print(f"backlog dashboard: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down backlog dashboard")
    finally:
        server.server_close()


def make_backlog_server(
    config: ProjectConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh_ms: int = 3000,
) -> ThreadingHTTPServer:
    validate_serve_options(host=host, port=port, refresh_ms=refresh_ms)
    server_cls = _ipv6_server_class() if host == "::1" else ThreadingHTTPServer
    handler = _handler_factory(config, refresh_ms)
    try:
        return server_cls((host, port), handler)
    except OSError as exc:
        raise ServerError(f"could not bind {host}:{port}: {exc}") from exc


def validate_serve_options(*, host: str, port: int, refresh_ms: int) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ServerError("backlog serve accepts loopback hosts only: 127.0.0.1, localhost, ::1")
    if port == 0:
        raise ServerError("backlog serve does not support --port 0 in v2.2")
    if port < 1 or port > 65535:
        raise ServerError("--port must be between 1 and 65535")
    if refresh_ms < 1000:
        raise ServerError("--refresh-ms must be at least 1000")


def dashboard_url(host: str, port: int) -> str:
    if host == "::1":
        return f"http://[::1]:{port}"
    return f"http://{host}:{port}"


def _handler_factory(config: ProjectConfig, refresh_ms: int) -> type[BaseHTTPRequestHandler]:
    class BacklogHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            route = PurePosixPath(urlparse(self.path).path).as_posix()
            if route == "/":
                self._send_text("text/html; charset=utf-8", dashboard_html(refresh_ms))
                return
            if route == "/api/health":
                self._send_json({"ok": True})
                return
            if route == "/api/backlog.json":
                snapshot = build_backlog_snapshot(config)
                self._send_json(backlog_snapshot_to_dict(snapshot))
                return
            self._send_text("text/plain; charset=utf-8", "not found\n", status=404)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, content_type: str, text: str, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return BacklogHandler


def _ipv6_server_class() -> type[ThreadingHTTPServer]:
    class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    return IPv6ThreadingHTTPServer


def dashboard_html(refresh_ms: int) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WorkerPool Backlog</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f6f8;
      --surface: #ffffff;
      --line: #d7dce2;
      --text: #20242a;
      --muted: #68717d;
      --badge: #eef1f4;
      --accent: #275c7a;
      --danger: #9d2d34;
      --warn: #8a5b12;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Arial, sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.96);
      padding: 12px 16px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 18px;
      font-weight: 650;
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 6px 14px;
      color: var(--muted);
      font-size: 12px;
    }}
    .meta span {{
      overflow-wrap: anywhere;
    }}
    main {{
      padding: 14px 16px 18px;
    }}
    .validation {{
      display: grid;
      grid-template-columns: repeat(2, minmax(240px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .panel {{
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 6px;
      padding: 10px;
      min-height: 44px;
    }}
    .panel h2 {{
      margin: 0 0 6px;
      font-size: 13px;
    }}
    .panel ul {{
      margin: 0;
      padding-left: 18px;
    }}
    .board {{
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: minmax(260px, 320px);
      gap: 10px;
      overflow-x: auto;
      padding-bottom: 12px;
    }}
    .column {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #eef1f4;
      min-height: 220px;
    }}
    .column-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      font-weight: 650;
      font-size: 13px;
    }}
    .count {{
      min-width: 24px;
      border-radius: 999px;
      background: var(--surface);
      padding: 2px 7px;
      text-align: center;
      color: var(--muted);
      font-size: 12px;
    }}
    .features {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 8px;
    }}
    .feature {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
    }}
    summary {{
      cursor: pointer;
      padding: 9px;
    }}
    .feature-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 5px;
      font-weight: 650;
    }}
    .subtitle {{
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: var(--badge);
      padding: 2px 7px;
      font-size: 11px;
      white-space: nowrap;
    }}
    .badge.failed,
    .badge.worker_failed {{
      background: #f8d7da;
      color: var(--danger);
    }}
    .badge.running {{
      background: #d7ecf7;
      color: var(--accent);
    }}
    .badge.worker_succeeded {{
      background: #fff3cd;
      color: var(--warn);
    }}
    .badge.merged {{
      background: #d8f0df;
      color: #23603a;
    }}
    .feature-body {{
      border-top: 1px solid var(--line);
      padding: 8px 9px 9px;
    }}
    .note {{
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .note.warn {{ color: var(--warn); }}
    .note.danger {{ color: var(--danger); }}
    .tasks {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .task {{
      border-top: 1px solid var(--line);
      padding-top: 6px;
    }}
    .task:first-child {{
      border-top: 0;
      padding-top: 0;
    }}
    .task-main {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 13px;
      font-weight: 600;
    }}
    .task-grid {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 2px 8px;
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
    }}
    .task-grid div {{
      overflow-wrap: anywhere;
    }}
    .empty {{
      padding: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .unassigned {{
      margin-top: 2px;
    }}
    @media (max-width: 760px) {{
      .meta,
      .validation {{
        grid-template-columns: 1fr;
      }}
      main {{
        padding: 10px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>WorkerPool Backlog</h1>
    <div class="meta">
      <span>Repo: <strong id="repo">loading</strong></span>
      <span>Pool: <strong id="pool">loading</strong></span>
      <span>Updated: <strong id="updated">never</strong></span>
      <span>Status: <strong id="status">connecting</strong></span>
    </div>
  </header>
  <main>
    <section class="validation">
      <div class="panel">
        <h2>Validation Errors</h2>
        <ul id="errors"></ul>
      </div>
      <div class="panel">
        <h2>Validation Warnings</h2>
        <ul id="warnings"></ul>
      </div>
    </section>
    <section id="board" class="board" aria-label="Kanban board"></section>
    <section class="panel unassigned">
      <h2>Unassigned</h2>
      <div id="unassigned"></div>
    </section>
  </main>
  <script>
    const refreshMs = {refresh_ms};
    let lastSnapshot = null;

    function text(value) {{
      return value === null || value === undefined || value === '' ? '-' : String(value);
    }}

    function el(tag, className, value) {{
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (value !== undefined) node.textContent = text(value);
      return node;
    }}

    function renderList(target, items) {{
      target.replaceChildren();
      if (!items.length) {{
        target.appendChild(el('li', '', 'None'));
        return;
      }}
      for (const item of items) target.appendChild(el('li', '', item));
    }}

    function renderTask(task) {{
      const root = el('div', 'task');
      const main = el('div', 'task-main');
      main.appendChild(el('span', '', task.task_id + ' ' + task.title));
      main.appendChild(el('span', 'badge', task.kind || 'implementation'));
      main.appendChild(el('span', 'badge', task.executor || 'worker'));
      main.appendChild(el('span', 'badge ' + text(task.execution_status), task.execution_status));
      root.appendChild(main);
      const grid = el('div', 'task-grid');
      const dependsOn = (task.depends_on || []).join(', ');
      const declaredDependsOn = (task.declared_depends_on || []).join(', ');
      const effectiveDependsOn = (task.effective_depends_on || []).join(', ');
      const blockers = (task.blockers || []).join('; ');
      const reviewFindings = (task.review_findings || []).join('; ');
      const withdrawnReplacements = (task.withdrawn_replacement_tasks || []).join(', ');
      const replacementChain = (task.replacement_chain || []).join(' -> ');
      const sourceBranches = (task.source_branches || []).join(', ');
      const mergeOrder = (task.merge_order || []).join(', ');
      const pairs = [
        ['plan', task.plan_status],
        ['kind', task.kind],
        ['executor', task.executor],
        ['depends_on', dependsOn],
        ['declared_depends_on', declaredDependsOn],
        ['effective_depends_on', effectiveDependsOn],
        ['blocked_by', blockers],
        ['review_findings', reviewFindings],
        ['superseded_by', task.superseded_by],
        ['replacement_contract', task.replacement_contract],
        ['replacement_chain', replacementChain],
        ['replaces', task.replaces],
        ['superseded_reason', task.superseded_reason],
        ['withdrawn_reason', task.withdrawn_reason],
        ['withdrawn_replacements', withdrawnReplacements],
        ['worker', task.worker],
        ['base_branch', task.base_branch],
        ['target_branch', task.target_branch],
        ['integration_result', task.integration_result],
        ['finish_destination', task.finish_destination],
        ['source_branches', sourceBranches],
        ['merge_order', mergeOrder],
        ['branch_ahead', task.branch_ahead_count],
        ['branch', task.branch],
        ['worktree', task.worktree],
        ['exit', task.exit_code],
        ['setup_exit', task.setup_exit_code],
        ['setup', task.setup_command],
        ['allowed', task.allowed_files_count],
        ['log', task.log_path],
        ['review', task.review_diff_path],
        ['review_hash', task.review_snapshot_hash],
        ['final', task.final_diff_path],
      ];
      for (const [key, value] of pairs) {{
        grid.appendChild(el('div', '', key));
        grid.appendChild(el('div', '', value));
      }}
      root.appendChild(grid);
      return root;
    }}

    function renderFeature(feature) {{
      const details = el('details', 'feature');
      details.open = true;
      const summary = el('summary');
      const title = el('div', 'feature-title');
      title.appendChild(el('span', '', feature.feature_id));
      title.appendChild(el('span', 'badge', feature.status));
      summary.appendChild(title);
      summary.appendChild(el('div', 'subtitle', feature.title));
      details.appendChild(summary);

      const body = el('div', 'feature-body');
      if (feature.blockers.length) body.appendChild(el('p', 'note danger', 'Blocked by: ' + feature.blockers.join('; ')));
      if (feature.open_decisions.length) body.appendChild(el('p', 'note warn', 'Open decisions: ' + feature.open_decisions.join(', ')));
      if (feature.review_findings.length) body.appendChild(el('p', 'note warn', 'Review findings: ' + feature.review_findings.join(', ')));
      if (feature.depends_on_features.length) body.appendChild(el('p', 'note', 'Depends on: ' + feature.depends_on_features.join(', ')));
      const tasks = el('div', 'tasks');
      if (feature.tasks.length) {{
        for (const task of feature.tasks) tasks.appendChild(renderTask(task));
      }} else {{
        tasks.appendChild(el('div', 'empty', 'No tasks'));
      }}
      body.appendChild(tasks);
      details.appendChild(body);
      return details;
    }}

    function renderSnapshot(snapshot) {{
      lastSnapshot = snapshot;
      document.getElementById('repo').textContent = text(snapshot.repo);
      document.getElementById('pool').textContent = text(snapshot.pool_root);
      document.getElementById('updated').textContent = text(snapshot.generated_at);
      document.getElementById('status').textContent = 'connected';
      renderList(document.getElementById('errors'), snapshot.validation_errors || []);
      renderList(document.getElementById('warnings'), snapshot.validation_warnings || []);

      const board = document.getElementById('board');
      board.replaceChildren();
      for (const column of snapshot.columns || []) {{
        const col = el('section', 'column');
        const header = el('div', 'column-header');
        header.appendChild(el('span', '', column.title));
        header.appendChild(el('span', 'count', column.features.length));
        col.appendChild(header);
        const features = el('div', 'features');
        if (column.features.length) {{
          for (const feature of column.features) features.appendChild(renderFeature(feature));
        }} else {{
          features.appendChild(el('div', 'empty', 'No features'));
        }}
        col.appendChild(features);
        board.appendChild(col);
      }}

      const unassigned = document.getElementById('unassigned');
      unassigned.replaceChildren();
      if (snapshot.unassigned_tasks && snapshot.unassigned_tasks.length) {{
        const tasks = el('div', 'tasks');
        for (const task of snapshot.unassigned_tasks) tasks.appendChild(renderTask(task));
        unassigned.appendChild(tasks);
      }} else {{
        unassigned.appendChild(el('div', 'empty', 'No unassigned tasks'));
      }}
    }}

    async function refresh() {{
      try {{
        const response = await fetch('/api/backlog.json', {{ cache: 'no-store' }});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        renderSnapshot(await response.json());
      }} catch (error) {{
        document.getElementById('status').textContent = 'refresh error: ' + error.message;
        if (!lastSnapshot) {{
          renderSnapshot({{
            repo: '-',
            pool_root: '-',
            generated_at: '-',
            columns: [],
            unassigned_tasks: [],
            validation_errors: ['Initial refresh failed: ' + error.message],
            validation_warnings: []
          }});
        }}
      }}
    }}

    refresh();
    setInterval(refresh, refreshMs);
  </script>
</body>
</html>
"""
