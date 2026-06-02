from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cowp.config import default_config_data, write_json


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.invalid"], repo)
    run(["git", "config", "user.name", "Test User"], repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_example.py").write_text("def test_example():\n    assert True\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)
    return repo


@pytest.fixture
def workerpool_config(git_repo: Path) -> Path:
    cfg = default_config_data(git_repo)
    cfg["acceptance"] = {"worker": None, "main": None}
    path = git_repo / ".codex-workerpool" / "config.json"
    write_json(path, cfg)
    return path


@pytest.fixture
def fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / ("opencode.cmd" if os.name == "nt" else "opencode")
    helper = bin_dir / "fake_opencode.py"
    helper.write_text(
        """
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
workdir = Path(args[args.index("--dir") + 1])
prompt = os.environ.get("COWP_PROMPT_TEXT") or "\\n".join(args)
task = "TASK-000"
for token in prompt.split():
    if token.startswith("TASK-"):
        task = token.strip("`.,:")
        break
if "SLEEP" in prompt:
    time.sleep(1.0)
target = None
for line in prompt.splitlines():
    if line.startswith("WRITE "):
        target = line.split(" ", 1)[1].strip()
        break
if target:
    path = workdir / target
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + f"# {task}\\n", encoding="utf-8")
payload = {"type": "text", "task": task, "target": target}
if "UNICODE" in prompt:
    payload["message"] = "多级目录 AI/Python"
text = json.dumps(payload, ensure_ascii=False) + "\\n"
sys.stdout.buffer.write(text.encode("utf-8"))
""".lstrip(),
        encoding="utf-8",
    )
    if os.name == "nt":
        script.write_text(f"@echo off\r\n\"{sys.executable}\" \"{helper}\" %*\r\n", encoding="utf-8")
    else:
        script.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv=[{str(helper)!r}, *sys.argv[1:]]\nrunpy.run_path({str(helper)!r}, run_name='__main__')\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return script


def write_manifest(repo: Path, tasks: list[dict]) -> Path:
    manifest = repo / ".codex-workerpool" / "tasks.json"
    write_json(manifest, {"tasks": tasks})
    for task in tasks:
        prompt = repo / task["prompt_file"]
        prompt.parent.mkdir(parents=True, exist_ok=True)
        target = task["allowed_files"][0] if task["allowed_files"] else "src/example.py"
        prompt.write_text(
            f"# {task['id']} {task['title']}\n\nRead WORKER_PROTOCOL.md.\n\nWRITE {target}\n",
            encoding="utf-8",
        )
    run(["git", "add", ".codex-workerpool"], repo)
    run(["git", "commit", "-m", "add workerpool manifest"], repo)
    return manifest
