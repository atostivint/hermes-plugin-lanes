"""Adapters: the seam that keeps "lanes" from being "the Paseo thing".

A lane delegates to an adapter, and an adapter is a small object with one job:
turn a Kanban card into a running agent somewhere else, and report on it
without pretending. Implement these six methods and a new harness works with
the same board, the same audit trail and the same dashboard tab.

    available()   -> (bool, reason)          can this adapter run right now?
    workspaces()  -> {local_path: handle}    where it can work
    launch()      -> {"handle", "provider", "workspace"}
    status()      -> {"state", "updates", "tokens", "detail", "pending"}
    activity()    -> str                     human-readable transcript tail
    cancel()      -> bool

`state` is deliberately tiny: "running" | "idle" | "error" | "gone". Anything
richer is the adapter's business; the engine only decides whether to wait,
close or block.

Writing a CLI adapter later (claude, codex, opencode): spawn the CLI in the
card's workspace with the brief on stdin/argv, keep the pid as the handle,
map "process alive" -> running, "exit 0" -> idle, "exit != 0" -> error, and
attach the last N KB of the process log as activity. The engine's two-tick
confirmation still applies, which covers CLI tools that exit before flushing
their log.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional


class AdapterError(RuntimeError):
    pass


class Adapter:
    """Contract only. Subclass and implement every method."""

    id = "base"
    label = "Base"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def workspaces(self) -> dict[str, str]:
        return {}

    def launch(self, task, brief: str, *, provider: str, mode: str) -> dict:
        raise AdapterError(f"{self.id}: launch() not implemented")

    def status(self, handle: str) -> dict:
        raise AdapterError(f"{self.id}: status() not implemented")

    def activity(self, handle: str) -> str:
        return ""

    def cancel(self, handle: str) -> bool:
        return False


# --- Paseo ------------------------------------------------------------------

DEFAULT_HOME = os.path.expanduser("~/.hermes")
CACHE_CANDIDATES = (
    "bws_cache.json",
    "cache/bws_cache.json",
    "state/bws_cache.json",
)


def _norm_path(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return raw.replace("\\\\", "\\").replace("/", "\\").rstrip("\\").lower()


def paseo_token(home: Optional[str] = None) -> str:
    """PASEO_PASSWORD from the environment, else the BWS cache. Never printed."""
    value = os.environ.get("PASEO_PASSWORD")
    if value:
        return value
    root = home or os.environ.get("HERMES_HOME") or DEFAULT_HOME
    tried = []
    for rel in CACHE_CANDIDATES:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8") as fh:
                secret = (json.load(fh).get("secrets") or {}).get("PASEO_PASSWORD")
        except (OSError, ValueError):
            tried.append(path)
            continue
        if secret:
            return secret
        tried.append(path)
    raise AdapterError("PASEO_PASSWORD not found; looked in " + ", ".join(tried))


class PaseoAdapter(Adapter):
    """Agents running in a Paseo instance, reached over its MCP endpoint.

    `url` comes from the lane's `adapter_url`, else `$PASEO_MCP_URL`. There is no
    built-in host: an adapter with no endpoint reports itself unavailable instead
    of guessing where your agents live.
    """

    id = "paseo"
    label = "Paseo"

    def __init__(self, url: Optional[str] = None, home: Optional[str] = None, timeout: int = 90):
        self.url = (url or os.environ.get("PASEO_MCP_URL") or "").strip()
        self.home = home
        self.timeout = timeout

    # -- transport ---------------------------------------------------------
    def call(self, tool: str, args: Optional[dict] = None) -> dict:
        if not self.url:
            raise AdapterError("paseo: no endpoint configured (lane.adapter_url or PASEO_MCP_URL)")
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }).encode()
        request = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {paseo_token(self.home)}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise AdapterError(f"paseo {tool}: HTTP {exc.code}") from exc
        except Exception as exc:
            raise AdapterError(f"paseo {tool}: {type(exc).__name__}: {exc}") from exc
        lines = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        data = json.loads(lines[-1] if lines else body)
        result = data.get("result") or {}
        if result.get("isError"):
            text = " ".join(part.get("text", "") for part in result.get("content") or [])
            raise AdapterError(f"paseo {tool}: {text.strip()[:600]}")
        if "structuredContent" in result:
            return result["structuredContent"]
        return {"text": " ".join(part.get("text", "") for part in result.get("content") or [])}

    # -- contract ----------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if not self.url:
            return False, "no endpoint configured (lane.adapter_url or PASEO_MCP_URL)"
        try:
            self.call("list_providers")
            return True, "reachable"
        except AdapterError as exc:
            return False, str(exc)

    def workspaces(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for ws in self.call("list_workspaces").get("workspaces") or []:
            key = _norm_path(ws.get("cwd"))
            if key and key not in mapping:
                mapping[key] = ws["workspaceId"]
        return mapping

    def modes(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for prov in self.call("list_providers").get("providers") or []:
            out[prov.get("id")] = [m.get("id") for m in prov.get("modes") or []]
        return out

    def launch(self, task, brief: str, *, provider: str, mode: str) -> dict:
        workspace = self.workspaces().get(_norm_path(task.workspace_path))
        if not workspace:
            raise AdapterError(f"no Paseo workspace for {task.workspace_path!r}")
        payload: dict[str, Any] = {
            "title": (task.title or task.id)[:60],
            "provider": provider,
            "initialPrompt": brief,
            "workspaceId": workspace,
        }
        allowed = self.modes().get(provider.split("/")[0]) or []
        if mode and mode in allowed:
            payload["settings"] = {"modeId": mode}
        created = self.call("create_agent", payload)
        handle = created.get("agentId") or created.get("id")
        if not handle:
            raise AdapterError(f"create_agent returned no agentId: {str(created)[:300]}")
        return {"handle": handle, "provider": provider, "workspace": workspace}

    def status(self, handle: str) -> dict:
        try:
            state = self.call("get_agent_status", {"agentId": handle})
        except AdapterError as exc:
            if "not found" in str(exc).lower():
                return {"state": "gone", "updates": 0, "tokens": 0, "detail": str(exc), "pending": []}
            raise
        snap = state.get("snapshot", state) if isinstance(state, dict) else {}
        raw = (snap.get("status") or "").lower()
        usage = snap.get("lastUsage") or {}
        detail = ""
        if raw == "error":
            detail = str(snap.get("lastError") or "")
        return {
            "state": {
                "running": "running", "starting": "running", "queued": "running", "waiting": "running",
                "idle": "idle",
                "error": "error", "closed": "error", "cancelled": "error",
            }.get(raw, "running" if not raw else "error"),
            "updates": 0,
            "tokens": int(usage.get("outputTokens") or 0),
            "detail": detail,
            "pending": snap.get("pendingPermissions") or [],
            "raw": raw,
        }

    def activity(self, handle: str) -> str:
        try:
            data = self.call("get_agent_activity", {"agentId": handle})
        except AdapterError:
            return ""
        self._last_updates = int(data.get("updateCount") or 0)
        return data.get("content") or ""

    def updates(self, handle: str) -> int:
        """Update counter from the last activity() call (Paseo-only signal)."""
        return int(getattr(self, "_last_updates", 0))

    def cancel(self, handle: str) -> bool:
        for tool in ("cancel_agent", "kill_agent"):
            try:
                self.call(tool, {"agentId": handle})
                return True
            except AdapterError:
                continue
        return False


ADAPTERS: dict[str, type[Adapter]] = {PaseoAdapter.id: PaseoAdapter}


def get(adapter_id: str, **kwargs) -> Adapter:
    cls = ADAPTERS.get(adapter_id)
    if cls is None:
        raise AdapterError(f"unknown adapter {adapter_id!r}; known: {sorted(ADAPTERS)}")
    return cls(**kwargs)


def register(cls: type[Adapter]) -> None:
    ADAPTERS[cls.id] = cls
