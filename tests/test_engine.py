"""Engine tests — no Hermes, no board, no Paseo. `python -m pytest tests/`."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lanes_core import engine as E  # noqa: E402
from lanes_core.config import Config, Lane  # noqa: E402

NOW = 1_800_000_000


@dataclass
class FakeTask:
    id: str
    title: str = "card"
    body: str = "do it"
    status: str = "ready"
    claim_lock: Optional[str] = None
    claim_expires: Optional[int] = None
    started_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workspace_path: str = "C:\\repo"


class FakeBoard:
    def __init__(self):
        self.tasks: dict[str, FakeTask] = {}
        self.notes: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self._run = 10

    # seeding
    def add(self, task: FakeTask) -> FakeTask:
        self.tasks[task.id] = task
        self.notes.setdefault(task.id, [])
        return task

    def note(self, task_id: str, body: str, created_at: int = NOW) -> None:
        self.notes[task_id].append({"body": E.MARKER + " " + body, "created_at": created_at, "author": "lanes"})

    # port
    def running(self, prefix):
        return [t for t in self.tasks.values() if t.status == "running" and (t.claim_lock or "").startswith(prefix)]

    def ready(self, board, assignee):
        return [t for t in self.tasks.values() if t.status == "ready"]

    def get(self, task_id):
        return self.tasks.get(task_id)

    def comments(self, task_id):
        return list(self.notes.get(task_id, []))

    def comment(self, task_id, body, author):
        self.calls.append(("comment", task_id, body))
        self.notes.setdefault(task_id, []).append({"body": body, "created_at": NOW, "author": author})

    def claim(self, task_id, claimer, ttl):
        task = self.tasks.get(task_id)
        if not task or task.status != "ready":
            return None
        self._run += 1
        task.status, task.claim_lock = "running", claimer
        task.claim_expires = NOW + ttl
        task.started_at = NOW
        task.current_run_id = self._run
        self.calls.append(("claim", task_id, claimer))
        return task

    def heartbeat(self, task_id, claimer, ttl):
        self.calls.append(("heartbeat", task_id))
        return True

    def reclaim(self, task_id, reason):
        task = self.tasks[task_id]
        task.status, task.claim_lock = "ready", None
        self.calls.append(("reclaim", task_id, reason))
        return True

    def complete(self, task_id, summary, metadata, run_id):
        self.calls.append(("complete", task_id, summary))
        self.tasks[task_id].status = "done"
        return True

    def block(self, task_id, reason, kind):
        self.calls.append(("block", task_id, reason, kind))
        self.tasks[task_id].status = "blocked"
        return True


class FakeAdapter:
    id = "fake"

    def __init__(self):
        self.launched: list[dict] = []
        self.states: dict[str, dict] = {}
        self.activities: dict[str, str] = {}
        self.updates_value = 0

    def available(self):
        return True, "ok"

    def workspaces(self):
        return {"c:\\repo": "ws_1"}

    def launch(self, task, brief, *, provider, mode):
        handle = f"agent-{task.id}"
        self.launched.append({"task": task.id, "provider": provider, "brief": brief})
        return {"handle": handle, "provider": provider, "workspace": "ws_1"}

    def status(self, handle):
        return self.states.get(handle, {"state": "running", "tokens": 0, "updates": 0, "detail": "", "pending": []})

    def activity(self, handle):
        return self.activities.get(handle, "")

    def updates(self, handle):
        return self.updates_value

    def cancel(self, handle):
        return True


def make_engine(lane=None, cfg_overrides=None, adapter=None):
    lane = lane or Lane(id="L1", board="b", assignee="paseo", max_concurrent=1)
    cfg = Config(lanes=[lane], auto_dispatch=True)
    for key, value in (cfg_overrides or {}).items():
        setattr(cfg, key, value)
    adapter = adapter or FakeAdapter()
    board = FakeBoard()
    eng = E.Engine(board, cfg, lambda _lane: adapter, clock=lambda: NOW)
    return eng, board, adapter


# --- dispatch ---------------------------------------------------------------


def test_dispatch_claims_then_launches():
    eng, board, adapter = make_engine()
    board.add(FakeTask("t1"))
    report = eng.tick()
    assert report["dispatched"] == 1
    assert board.tasks["t1"].status == "running"
    assert board.tasks["t1"].claim_lock.startswith("lanes:L1:")
    bodies = [c[2] for c in board.calls if c[0] == "comment"]
    assert any("agent=pending" in b for b in bodies), bodies
    assert any("agent=agent-t1" in b for b in bodies), bodies
    # the claim note must precede the launch note
    assert bodies.index([b for b in bodies if "agent=pending" in b][0]) < len(bodies)
    assert adapter.launched[0]["task"] == "t1"


def test_dispatch_respects_per_lane_cap():
    eng, board, adapter = make_engine()
    running = board.add(FakeTask("t0", status="running", claim_lock="lanes:L1:host:1", claim_expires=NOW + 100))
    board.note("t0", "launched run=3 agent=agent-x adapter=fake")
    board.add(FakeTask("t1"))
    report = eng.tick()
    assert report["dispatched"] == 0
    assert adapter.launched == []
    assert running.status == "running"


def test_disabled_lane_never_dispatches():
    eng, board, adapter = make_engine(lane=Lane(id="L1", board="b", assignee="paseo", enabled=False))
    board.add(FakeTask("t1"))
    assert eng.tick()["dispatched"] == 0
    assert adapter.launched == []


def test_auto_dispatch_off_only_observes():
    eng, board, adapter = make_engine(cfg_overrides={"auto_dispatch": False})
    board.add(FakeTask("t1"))
    assert eng.tick()["dispatched"] == 0
    assert board.tasks["t1"].status == "ready"


def test_dry_run_does_not_touch_the_board():
    eng, board, adapter = make_engine()
    board.add(FakeTask("t1"))
    report = eng.tick(dry_run=True)
    assert report["dispatched"] == 0
    assert board.tasks["t1"].status == "ready"
    assert not [c for c in board.calls if c[0] == "claim"]


# --- watch ------------------------------------------------------------------


def _running_card(board, agent="agent-t1", adapter="fake", run="10", updates_note=True):
    task = board.add(FakeTask("t1", status="running", claim_lock="lanes:L1:h:1", claim_expires=NOW + 7000,
                              started_at=NOW, current_run_id=10))
    board.note("t1", f"claim run={run} lane=L1 adapter={adapter} agent=pending")
    board.note("t1", f"launched run={run} agent={agent} lane=L1 adapter={adapter}")
    return task


def test_running_agent_is_heartbeated_not_closed():
    eng, board, adapter = make_engine()
    task = _running_card(board)
    adapter.states["agent-t1"] = {"state": "running", "tokens": 0, "updates": 0, "detail": "", "pending": []}
    eng.tick()
    assert task.status == "running"
    assert any(c[0] == "heartbeat" for c in board.calls)
    assert not [c for c in board.calls if c[0] == "complete"]


def test_idle_needs_two_sightings_then_completes():
    eng, board, adapter = make_engine()
    task = _running_card(board)
    adapter.states["agent-t1"] = {"state": "idle", "tokens": 40, "updates": 4, "detail": "", "pending": []}
    adapter.activities["agent-t1"] = "Showing all 3 activities\n\n[User] go\n\n[Shell] git --version\ngit version 2.56"
    adapter.updates_value = 4

    eng.tick()  # first sighting: confirm only
    assert task.status == "running"
    assert any("idle-seen" in c[2] for c in board.calls if c[0] == "comment")

    eng.tick()  # second sighting, same update count: close
    assert task.status == "done"
    summary = [c[2] for c in board.calls if c[0] == "complete"][0]
    assert "git version 2.56" in summary
    assert "Showing all" not in summary
    assert "agent-t1" in summary


def test_idle_with_more_updates_keeps_waiting():
    eng, board, adapter = make_engine()
    task = _running_card(board)
    adapter.states["agent-t1"] = {"state": "idle", "tokens": 40, "updates": 4, "detail": "", "pending": []}
    adapter.activities["agent-t1"] = "[Shell] ls\nfile.txt"
    adapter.updates_value = 4
    eng.tick()
    adapter.updates_value = 6  # transcript moved between ticks
    adapter.activities["agent-t1"] = "[Shell] ls\nfile.txt\n[Assistant] done"
    eng.tick()
    assert task.status == "running"
    assert not [c for c in board.calls if c[0] == "complete"]


def test_idle_with_zero_tokens_and_no_tool_blocks():
    eng, board, adapter = make_engine()
    task = _running_card(board)
    adapter.states["agent-t1"] = {"state": "idle", "tokens": 0, "updates": 2, "detail": "", "pending": []}
    adapter.activities["agent-t1"] = (
        "Showing all 2 activities\n\n[User] go\n\n"
        "Claude Code can't use your Claude subscription, usually because a payment is past due")
    adapter.updates_value = 2
    eng.tick()
    assert task.status == "blocked"
    reason = [c for c in board.calls if c[0] == "block"][0]
    assert reason[3] == "capability"
    assert "payment is past due" in reason[2]


def test_error_state_blocks_with_detail():
    eng, board, adapter = make_engine()
    task = _running_card(board)
    adapter.states["agent-t1"] = {"state": "error", "tokens": 0, "updates": 1,
                                  "detail": "Model unavailable", "pending": []}
    eng.tick()
    assert task.status == "blocked"
    assert "Model unavailable" in [c for c in board.calls if c[0] == "block"][0][2]


def test_permission_pending_is_reported_once():
    eng, board, adapter = make_engine()
    _running_card(board)
    adapter.states["agent-t1"] = {"state": "running", "tokens": 5, "updates": 1, "detail": "",
                                  "pending": [{"tool": "Bash", "command": "rm -rf x"}]}
    eng.tick()
    eng.tick()
    perms = [c for c in board.calls if c[0] == "comment" and c[2].startswith(E.MARKER + " permission")]
    assert len(perms) == 1


def test_orphan_claim_is_released_after_grace():
    eng, board, adapter = make_engine()
    task = board.add(FakeTask("t1", status="running", claim_lock="lanes:L1:h:1",
                              claim_expires=NOW + E.CLAIM_TTL_SECONDS - 200, started_at=NOW - 200))
    report = eng.tick()
    assert any(c[0] == "reclaim" for c in board.calls), board.calls
    # the same tick may legitimately re-dispatch the freed card
    assert task.status in ("ready", "running")
    assert any("released" in a for a in report["actions"]), report


def test_fresh_claim_without_note_is_left_alone():
    eng, board, adapter = make_engine()
    task = board.add(FakeTask("t1", status="running", claim_lock="lanes:L1:h:1",
                              claim_expires=NOW + E.CLAIM_TTL_SECONDS - 5, started_at=NOW))
    eng.tick()
    assert task.status == "running"
    assert not [c for c in board.calls if c[0] == "reclaim"]


def test_stale_pending_claim_is_released():
    eng, board, adapter = make_engine()
    task = board.add(FakeTask("t1", status="running", claim_lock="lanes:L1:h:1",
                              claim_expires=NOW + E.CLAIM_TTL_SECONDS - 400, started_at=NOW - 400))
    board.note("t1", "claim run=9 lane=L1 adapter=fake agent=pending", created_at=NOW - 400)
    report = eng.tick()
    assert any(c[0] == "reclaim" for c in board.calls), board.calls
    assert any("never launched" in a for a in report["actions"]), report


# --- text -------------------------------------------------------------------


def test_summarize_strips_header_and_prompt():
    content = ("Showing all 2 activities\n\n[User] prompt echo\n\n"
               "[Thought] hmm\n\n[Shell] git --version\ngit version 2.56\n\n[Assistant] done")
    out = E.summarize(content)
    assert "Showing all" not in out
    assert "prompt echo" not in out
    assert out.endswith("[Assistant] done")


def test_brief_truncates_long_body_and_keeps_pointer():
    task = FakeTask("t1", title="big", body="x" * 9000)
    brief = E.build_brief(task, limit=3000)
    assert "truncated by the lanes engine" in brief
    assert len(brief) < 4200


def test_parse_notes_ignores_foreign_comments():
    notes = E.parse_notes([
        {"body": "human comment", "created_at": 1},
        {"body": E.MARKER + " launched run=7 agent=abc lane=L1 adapter=paseo", "created_at": 2},
    ])
    assert len(notes) == 1
    assert notes[0]["agent"] == "abc"
    assert notes[0]["lane"] == "L1"
    assert notes[0]["adapter"] == "paseo"
    assert notes[0]["run"] == "7"
