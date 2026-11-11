"""Managed local llama.cpp server.

When LOCAL_LLM_AUTOSTART is on and LOCAL_LLM_SERVER_PATH +
LOCAL_LLM_MODEL_PATH are set, spawns ``llama-server`` unless the port is
already healthy; waits for /health up to 90 s; logs to runs/llama-server.log;
terminates only the process it started. Without configured paths it just
probes the configured base URL.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class LlamaServerManager:
    def __init__(self, settings) -> None:
        self._cfg = settings
        self._proc: subprocess.Popen | None = None
        self._started = False
        self._log_handle = None

    # ── URL helpers ────────────────────────────────────────────────────────
    def _root(self) -> str:
        u = urlparse(self._cfg.local_llm_base_url)
        return f"{u.scheme}://{u.netloc}"

    def _health_url(self) -> str:
        return self._root() + "/health"

    def _port(self) -> int:
        u = urlparse(self._cfg.local_llm_base_url)
        return u.port or 8088

    # ── Probe ──────────────────────────────────────────────────────────────
    async def healthy(self, timeout: float = 2.0) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(self._health_url())
                return r.status_code == 200
        except Exception:
            return False

    # ── Command construction (unit-testable) ──────────────────────────────
    def command(self) -> list[str]:
        cfg = self._cfg
        cmd = [str(cfg.local_llm_server_path),
               "-m", str(cfg.local_llm_model_path),
               "-c", str(cfg.local_llm_ctx),
               "-ngl", "99",
               "-np", str(cfg.local_llm_parallel),
               "--port", str(self._port())]
        cmd += str(cfg.local_llm_extra_args or "").split()
        return cmd

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def start(self, wait_s: float = 90.0) -> bool:
        """Ensure a healthy local server; spawn if configured. → healthy?"""
        cfg = self._cfg
        if not cfg.local_llm_enabled:
            return False
        if await self.healthy():
            logger.info("llama-server already healthy at %s", self._health_url())
            return True
        if not (cfg.local_llm_autostart and cfg.local_llm_server_path
                and cfg.local_llm_model_path):
            return False
        exe = Path(cfg.local_llm_server_path)
        if not exe.exists():
            logger.warning("llama-server not found: %s", exe)
            return False
        Path("runs").mkdir(exist_ok=True)
        self._log_handle = open("runs/llama-server.log", "ab", buffering=0)
        cmd = self.command()
        logger.info("spawning llama-server: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, stdout=self._log_handle, stderr=subprocess.STDOUT,
            cwd=str(Path.cwd()))
        self._started = True
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if await self.healthy():
                logger.info("llama-server healthy after %.0fs", wait_s -
                            (deadline - time.monotonic()))
                return True
            if self._proc.poll() is not None:
                logger.warning("llama-server exited early (rc=%s)", self._proc.returncode)
                return False
            await asyncio.sleep(1.0)
        logger.warning("llama-server not healthy within %ss", wait_s)
        return False

    async def stop(self) -> None:
        """Terminate only the process we spawned."""
        if self._proc is not None and self._started:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._started = False
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
