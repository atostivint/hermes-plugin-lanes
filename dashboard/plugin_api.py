"""Lanes — dashboard backend.

Mounted by the Hermes dashboard at /api/plugins/lanes/.

A "lane" is one (board, assignee) pair served by one adapter. The board stays the
source of truth: this file only wires `lanes_core` to the live kanban DB and runs
the tick on a background thread. Nothing here spawns a Hermes worker — cards
whose assignee does not resolve to a profile are skipped by the dispatcher on
purpose, and the lane is what picks them up.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from lanes_core import adapters as adapters_mod  # noqa: E402
from lanes_core import config as config_mod  # noqa: E402
from lanes_core import engine as engine_mod  # noqa: E402

DATA_DIR = PLUGIN_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
LOCK_FILE = DATA_DIR / "tick.lock"

router = APIRouter()

_state_lock = threading.Lock()
_last_tick: dict = {}
_ticker_started = False


# --- helpers ----------------------------------------------------------------


def load_config():
    return config_mod.load(DATA_DIR)


def save_config(cfg) -> None:
    config_mod.save(DATA_DIR, cfg)


def _board():
    from lanes_core.kanban_hermes import HermesBoard

    cfg = load_config()
    return HermesBoard([lane.board for lane in cfg.lanes])


def build_engine():
    cfg = load_config()

    def factory(lane):
        return adapters_mod.get(lane.adapter, url=(getattr(lane, "adapter_url", "") or None))

    return engine_mod.Engine(_board(), cfg, factory), cfg


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def _write_state(payload: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def run_tick(dry_run: bool = False) -> dict:
    """One full tick, serialised across threads and processes."""
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return {"skipped": True, "reason": "another tick is running", "actions": [], "errors": []}

    try:
        eng, _cfg = build_engine()
        report = eng.tick(dry_run=dry_run)
    except Exception as exc:
        report = {"started_at": time.time(), "actions": [], "errors": [f"{type(exc).__name__}: {exc}"],
                  "dispatched": 0, "closed": 0, "blocked": 0, "duration_ms": 0}
    finally:
        try:
            eng.kanban.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    with _state_lock:
        global _last_tick
        _last_tick = report
        state = _read_state()
        state["last_tick"] = report
        _write_state(state)

    # Durable tick journal: who did what, from which pid. A lane that claims
    # without leaving a comment is a bug, and the journal is how it gets caught.
    try:
        with open(DATA_DIR / "tick.log", "a", encoding="utf-8") as fh:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            for line in report.get("actions") or []:
                fh.write(f"{stamp} pid={os.getpid()} {line}\n")
            for line in report.get("errors") or []:
                fh.write(f"{stamp} pid={os.getpid()} ERROR {line}\n")
    except OSError:
        pass
    return report


def _ticker_loop() -> None:
    while True:
        cfg = load_config()
        try:
            run_tick()
        except Exception:
            pass
        time.sleep(max(15, int(cfg.tick_seconds)))


def ensure_ticker() -> None:
    """Start the background ticker once per process."""
    global _ticker_started
    if _ticker_started:
        return
    _ticker_started = True
    thread = threading.Thread(target=_ticker_loop, name="lanes-ticker", daemon=True)
    thread.start()


# The dashboard imports this module to mount the router, so the lane runs whether
# or not anyone opens the tab. The thread sleeps one interval before its first
# tick, which keeps the HTTP mount fast.
ensure_ticker()


# --- payloads ---------------------------------------------------------------


class EnabledBody(BaseModel):
    enabled: bool


class ConfigBody(BaseModel):
    auto_dispatch: Optional[bool] = None
    tick_seconds: Optional[int] = None
    lane: Optional[dict] = None


def _job_rows(board, cfg) -> list[dict]:
    rows = []
    for lane in cfg.lanes:
        for task in board.running(lane.claim_prefix()):
            notes = engine_mod.parse_notes(board.comments(task.id))
            note = engine_mod.latest_agent_note(notes) or {}
            rows.append({
                "task_id": task.id,
                "board": getattr(task, "_lanes_board", lane.board),
                "title": task.title,
                "lane": lane.id,
                "adapter": note.get("adapter") or lane.adapter,
                "handle": note.get("agent") or "?",
                "run": note.get("run"),
                "status": task.status,
                "started_at": task.started_at,
                "claim_expires": task.claim_expires,
                "elapsed_s": int(time.time() - (task.started_at or time.time())),
            })
    return rows


# --- routes -----------------------------------------------------------------


@router.get("/state")
async def state(probe: bool = False):
    ensure_ticker()
    cfg = load_config()

    def build() -> dict:
        board = _board()
        try:
            lanes = []
            for lane in cfg.lanes:
                entry = {
                    "id": lane.id, "adapter": lane.adapter, "board": lane.board,
                    "assignee": lane.assignee, "provider": lane.provider, "mode": lane.mode,
                    "max_concurrent": lane.max_concurrent, "enabled": lane.enabled,
                    "ready": len(board.ready(lane.board, lane.assignee)),
                    "running": len(board.running(lane.claim_prefix())),
                }
                if probe:
                    try:
                        adapter = adapters_mod.get(
                            lane.adapter, url=(getattr(lane, "adapter_url", "") or None))
                        entry["adapter_ok"], entry["adapter_detail"] = adapter.available()
                    except Exception as exc:
                        entry["adapter_ok"], entry["adapter_detail"] = False, f"{type(exc).__name__}: {exc}"
                lanes.append(entry)
            return {
                "lanes": lanes,
                "jobs": _job_rows(board, cfg),
                "counts": {b: board.counts(b) for b in dict.fromkeys(l.board for l in cfg.lanes)},
                "auto_dispatch": cfg.auto_dispatch,
                "tick_seconds": cfg.tick_seconds,
                "last_tick": _last_tick or _read_state().get("last_tick") or {},
            }
        finally:
            board.close()

    return await asyncio.to_thread(build)


@router.post("/tick")
async def tick(dry_run: bool = False):
    return await asyncio.to_thread(run_tick, dry_run)


@router.post("/lanes/{lane_id}/enabled")
async def set_enabled(lane_id: str, body: EnabledBody):
    cfg = load_config()
    lane = cfg.lane(lane_id)
    if lane is None:
        raise HTTPException(status_code=404, detail=f"unknown lane {lane_id}")
    lane.enabled = body.enabled
    save_config(cfg)
    return {"lane": lane_id, "enabled": lane.enabled}


@router.post("/config")
async def update_config(body: ConfigBody):
    cfg = load_config()
    if body.auto_dispatch is not None:
        cfg.auto_dispatch = body.auto_dispatch
    if body.tick_seconds is not None:
        cfg.tick_seconds = max(15, int(body.tick_seconds))
    if body.lane:
        lane = cfg.lane(str(body.lane.get("id", "")))
        if lane is None:
            raise HTTPException(status_code=404, detail="unknown lane")
        for key in ("provider", "mode", "max_concurrent", "assignee", "board"):
            if body.lane.get(key) is not None:
                setattr(lane, key, body.lane[key])
    save_config(cfg)
    return {"ok": True}


@router.post("/cards/{task_id}/cancel")
async def cancel_card(task_id: str):
    """Stop the external agent and park the card with the reason on it."""

    def work() -> dict:
        cfg = load_config()
        board = _board()
        try:
            task = board.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail=f"unknown card {task_id}")
            notes = engine_mod.parse_notes(board.comments(task_id))
            note = engine_mod.latest_agent_note(notes) or {}
            handle = note.get("agent")
            if not handle or handle == "pending":
                raise HTTPException(status_code=409, detail="no external agent recorded on this card")
            adapter = adapters_mod.get(note.get("adapter") or "paseo")
            stopped = adapter.cancel(handle)
            board.comment(task_id, f"{engine_mod.MARKER} cancelled by operator (agent {handle})", "lanes")
            board.block(task_id, f"lanes: cancelled by operator; adapter {adapter.id} agent {handle} "
                                 f"{'stopped' if stopped else 'not reachable'}", "needs_input")
            return {"task_id": task_id, "handle": handle, "stopped": stopped}
        finally:
            board.close()

    return await asyncio.to_thread(work)
