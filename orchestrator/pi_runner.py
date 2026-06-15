"""Единая точка вызова pi из Python.

Зовём `node …/cli.js` напрямую (не `pi.cmd`), чтобы CreateProcess получал
чистый argv и промпт со спецсимволами/переносами не ломался об cmd.exe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def resolve_pi() -> list[str]:
    node = shutil.which("node") or "node"
    pi = shutil.which("pi")
    base = Path(pi).parent if pi else Path(r"C:\nvm4w\nodejs")
    cli = base / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js"
    return [node, str(cli)]


def run_pi(prompt: str, *, provider: str, model: str, cwd: str,
           tools: str | None = None, extension: str | None = None,
           env: dict | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    cmd = resolve_pi() + ["-p", prompt, "--provider", provider, "--model", model, "--no-session"]
    if extension:
        cmd += ["-e", extension]
    if tools:
        cmd += ["-t", tools]
    return subprocess.run(
        cmd, env={**os.environ, **(env or {})}, cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
