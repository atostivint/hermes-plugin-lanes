"""The lane engine: watch what is running outside, dispatch what is waiting.

Design rules, learned the hard way on the cron prototype:

* the board owns the truth — every action goes through the kanban port, so
  `task_events` / `task_runs` read exactly like a native worker's;
* a claim is written *before* the external agent is launched, so a crash is
  visible as a `pending` note instead of a card stuck in `running`;
* `idle` is not success — an agent that spent no token and called no tool is a
  failed run, and closes the card as `blocked`;
* nothing is closed on a single sighting: Paseo's transcript lags its own
  status, so a close needs two consecutive ticks with the same update count.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Protocol

MARKER = "[lanes]"
FRESH_CLAIM_SECONDS = 90
PENDING_GRACE_SECONDS = 180
CLAIM_TTL_SECONDS = 7200

BLOCK_RE = re.compile(
    r"\n(?=\[(?:User|Assistant|Thought|Text|Message|Write|Edit|Shell|Read|Bash|Grep|Glob|Task)\])"
)
TOOL_RE = re.compile(r"^\[(Write|Edit|Shell|Read|Bash|Grep|Glob|Task)\]", re.M)
PROMPT_RE = re.compile(r"^\[(User|Thought)\]")


# --- board port -------------------------------------------------------------


class KanbanPort(Protocol):
    def running(self, claim_prefix: str) -> list: ...
    def ready(self, board: str, assignee: str) -> list: ...
    def get(self, task_id: str): ...
    def comments(self, task_id: str) -> list: ...
    def comment(self, task_id: str, body: str, author: str) -> None: ...
    def claim(self, task_id: str, claimer: str, ttl: int): ...
    def heartbeat(self, task_id: str, claimer: str, ttl: int) -> bool: ...
    def reclaim(self, task_id: str, reason: str) -> bool: ...
    def complete(self, task_id: str, summary: str, metadata: dict, run_id: Optional[int]) -> bool: ...
    def block(self, task_id: str, reason: str, kind: str) -> bool: ...


# --- text -------------------------------------------------------------------


def tail(text: str, limit: int = 1200) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


def summarize(content: str, limit: int = 1200) -> str:
    """The agent's last word: no header, no echoed prompt, last useful blocks."""
    text = (content or "").strip()
    if not text:
        return ""
    text = re.sub(r"^Showing all \d+ activities\s*", "", text)
    blocks = [b for b in BLOCK_RE.split(text) if b.strip()]
    keep = [b for b in blocks if not PROMPT_RE.match(b)]
    return tail("\n".join(keep[-3:]) if keep else text, limit)


def turn_failure(status: dict, content: str) -> Optional[str]:
    """A run that spent nothing and touched nothing did not do the work."""
    if int(status.get("tokens") or 0) > 0:
        return None
    if TOOL_RE.search(content or ""):
        return None
    if re.search(r"^\[(Assistant|Text|Message)\]", content or "", re.M):
        return None
    return tail(content, 600) or "agent finished with no output and no tool calls"


def build_brief(task, limit: int = 3000) -> str:
    body = (task.body or "").strip()
    if len(body) > limit:
        body = body[:limit] + "\n\n[... card body truncated by the lanes engine ...]"
    return (
        f"You are the worker for Hermes Kanban card {task.id}.\n"
        f"Title: {task.title}\n\n"
        "Work in the current workspace. Do the work NOW with tool calls, one file per\n"
        "call. Do NOT plan in prose and do NOT draft code in your final message.\n\n"
        f"--- card body ---\n{body}\n--- end card body ---\n\n"
        "Finish with a short summary: what you changed, how you verified it, what is\n"
        "still open. Never run terraform, never push or merge, never delete files\n"
        "(rename to .dup instead).\n"
    )


# --- notes ------------------------------------------------------------------


def parse_notes(comments: list) -> list[dict]:
    notes = []
    for row in comments:
        if isinstance(row, dict):
            body, created = row.get("body", ""), row.get("created_at", 0) or 0
        else:
            body, created = getattr(row, "body", ""), getattr(row, "created_at", 0) or 0
        if not body or not body.startswith(MARKER):
            continue
        note: dict[str, Any] = {"raw": body, "created_at": created}
        for key in ("run", "agent", "lane", "adapter"):
            found = re.search(rf"{key}=([A-Za-z0-9_:.-]+)", body)
            if found:
                note[key] = found.group(1)
        notes.append(note)
    return notes


def latest_agent_note(notes: list[dict]) -> Optional[dict]:
    for note in reversed(notes):
        if note.get("agent"):
            return note
    return None


# --- engine -----------------------------------------------------------------


@dataclass
class TickReport:
    started_at: float
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dispatched: int = 0
    closed: int = 0
    blocked: int = 0

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "actions": self.actions,
            "errors": self.errors,
            "dispatched": self.dispatched,
            "closed": self.closed,
            "blocked": self.blocked,
            "duration_ms": int((time.time() - self.started_at) * 1000),
        }


class Engine:
    def __init__(self, kanban: KanbanPort, config, adapter_factory, *, clock=time.time):
        self.kanban = kanban
        self.config = config
        self.adapter_factory = adapter_factory
        self.clock = clock
        self._adapters: dict[str, Any] = {}

    def adapter(self, lane):
        """Adapters are built per lane: the lane carries its own endpoint/config."""
        key = getattr(lane, "adapter", "paseo")
        if key not in self._adapters:
            self._adapters[key] = self.adapter_factory(lane)
        return self._adapters[key]

    # -- watch ------------------------------------------------------------
    def watch(self, report: TickReport) -> None:
        for lane in self.config.lanes:
            prefix = lane.claim_prefix()
            for task in self.kanban.running(prefix):
                try:
                    self._watch_one(lane, task, report)
                except Exception as exc:  # one bad card must not stop the tick
                    report.errors.append(f"{lane.id}/{task.id}: {type(exc).__name__}: {exc}")

    def _watch_one(self, lane, task, report: TickReport) -> None:
        comments = self.kanban.comments(task.id)
        notes = parse_notes(comments)
        note = latest_agent_note(notes)

        if not note:
            age = int(self.clock()) - int((task.claim_expires or 0) - CLAIM_TTL_SECONDS)
            if age < FRESH_CLAIM_SECONDS:
                return
            self.kanban.reclaim(task.id, "lanes: claim without an agent record")
            report.actions.append(f"{task.id}: orphaned claim ({age}s) released")
            return

        handle = note["agent"]
        if handle == "pending":
            self.kanban.heartbeat(task.id, task.claim_lock, CLAIM_TTL_SECONDS)
            age = int(self.clock()) - int(note.get("created_at") or 0)
            if age > PENDING_GRACE_SECONDS:
                self.kanban.reclaim(task.id, "lanes: claim never reached the adapter")
                report.actions.append(f"{task.id}: claim never launched ({age}s) released")
            return

        adapter_id = note.get("adapter") or lane.adapter
        adapter = self.adapter(replace(lane, adapter=adapter_id))
        status = adapter.status(handle)
        state = status.get("state")

        if state == "running":
            self.kanban.heartbeat(task.id, task.claim_lock, CLAIM_TTL_SECONDS)
            pending = status.get("pending") or []
            if pending and not any(n["raw"].startswith(f"{MARKER} permission") for n in notes):
                self.kanban.comment(
                    task.id,
                    f"{MARKER} permission pending on {handle}: {str(pending[0])[:400]}\n"
                    "Approve it on the adapter side; the claim stays alive.",
                    "lanes",
                )
                report.actions.append(f"{task.id}: permission pending")
            return

        content = adapter.activity(handle) if hasattr(adapter, "activity") else ""
        updates = int(getattr(adapter, "updates", lambda _h: 0)(handle) or 0) if state == "idle" else 0
        meta = {"lane": lane.id, "adapter": adapter.id, "handle": handle, "state": state}
        if note.get("run", "").isdigit():
            meta["run"] = int(note["run"])

        if state == "idle":
            failure = turn_failure(status, content)
            if failure:
                self.kanban.block(
                    task.id, f"lanes: {adapter.id} agent {handle} produced no work: {failure}", "capability"
                )
                report.blocked += 1
                report.actions.append(f"{task.id}: empty run -> blocked")
                return
            marker = f"{MARKER} idle-seen agent={handle} run={note.get('run', '')} updates={updates}"
            if not any(n["raw"].startswith(marker) for n in notes):
                self.kanban.comment(task.id, marker + " awaiting one more tick", "lanes")
                report.actions.append(f"{task.id}: idle (updates={updates}), awaiting confirmation")
                return
            summary = summarize(content, self.config.summary_limit) or "(agent returned no summary)"
            summary += f"\n\n[lanes] {adapter.id} {handle} · run={note.get('run', '')} · full transcript on the adapter"
            self.kanban.comment(task.id, f"{MARKER} closing run={note.get('run', '')} agent={handle}", "lanes")
            ok = self.kanban.complete(task.id, summary, meta, meta.get("run"))
            report.closed += 1 if ok else 0
            report.actions.append(f"{task.id}: closed (complete={ok})")
            return

        detail = str(status.get("detail") or "")[:600] or f"agent {state}"
        self.kanban.block(task.id, f"lanes: {adapter.id} agent {handle} {state}: {detail}", "capability")
        report.blocked += 1
        report.actions.append(f"{task.id}: agent {state} -> blocked")

    # -- dispatch ---------------------------------------------------------
    def dispatch(self, report: TickReport, *, dry_run: bool = False) -> None:
        if not self.config.auto_dispatch and not dry_run:
            return
        for lane in self.config.lanes:
            if not lane.enabled:
                continue
            try:
                self._dispatch_lane(lane, report, dry_run=dry_run)
            except Exception as exc:
                report.errors.append(f"{lane.id}: {type(exc).__name__}: {exc}")

    def _dispatch_lane(self, lane, report: TickReport, *, dry_run: bool) -> None:
        running = len(self.kanban.running(lane.claim_prefix()))
        slots = lane.max_concurrent - running
        if slots <= 0:
            return
        ready = self.kanban.ready(lane.board, lane.assignee)
        if not ready:
            return
        adapter = self.adapter(lane)
        for task in ready[:slots]:
            if dry_run:
                report.actions.append(f"{task.id}: dry-run would dispatch via {lane.adapter}")
                continue
            claimer = f"{lane.claim_prefix()}:{_host()}:{_pid()}"
            claimed = self.kanban.claim(task.id, claimer, CLAIM_TTL_SECONDS)
            if not claimed:
                continue
            run_id = getattr(claimed, "current_run_id", None)
            self.kanban.comment(
                task.id,
                f"{MARKER} claim run={run_id} lane={lane.id} adapter={lane.adapter} agent=pending",
                "lanes",
            )
            try:
                launched = adapter.launch(task, build_brief(task, self.config.brief_body_limit),
                                          provider=lane.provider, mode=lane.mode)
            except Exception as exc:
                self.kanban.block(task.id, f"lanes: {lane.adapter} launch failed: {exc}", "capability")
                report.blocked += 1
                report.errors.append(f"{task.id}: launch failed: {exc}")
                continue
            self.kanban.comment(
                task.id,
                f"{MARKER} launched run={run_id} agent={launched['handle']} "
                f"lane={lane.id} adapter={lane.adapter} provider={launched.get('provider')} "
                f"workspace={launched.get('workspace')}",
                "lanes",
            )
            report.dispatched += 1
            report.actions.append(f"{task.id}: launched {launched['handle']}")

    # -- entry ------------------------------------------------------------
    def tick(self, *, dry_run: bool = False) -> dict:
        started = self.clock()
        report = TickReport(started_at=started)
        self.watch(report)
        self.dispatch(report, dry_run=dry_run)
        payload = report.as_dict()
        payload["duration_ms"] = int((self.clock() - started) * 1000)
        return payload


def _host() -> str:
    import socket
    try:
        return socket.gethostname() or "host"
    except Exception:
        return "host"


def _pid() -> int:
    import os
    return os.getpid()
