"""The Hermes side of the engine — the only module that imports Hermes.

Everything else in `lanes_core` is stdlib-only, so the engine's tests never need
a live board. This file maps the engine's small board port onto
`hermes_cli.kanban_db`, which is the same layer the CLI, the dashboard and the
`kanban_*` tools use: writes cannot drift from theirs.
"""

from __future__ import annotations

from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


class HermesBoard:
    """Board port over one or more kanban boards, connections cached per board."""

    def __init__(self, boards: list[str]):
        self.boards = list(dict.fromkeys(b for b in boards if b))
        self._conns: dict[str, Any] = {}
        self._task_board: dict[str, str] = {}

    # -- plumbing ----------------------------------------------------------
    def conn(self, board: str):
        if board not in self._conns:
            self._conns[board] = connect(board=board)
        return self._conns[board]

    def close(self) -> None:
        for c in self._conns.values():
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()

    def board_of(self, task, default: Optional[str] = None) -> str:
        return getattr(task, "_lanes_board", None) or self._task_board.get(getattr(task, "id", "")) or default or self.boards[0]

    def _stamp(self, task, board: str):
        try:
            setattr(task, "_lanes_board", board)
        except Exception:
            pass
        self._task_board[task.id] = board
        return task

    # -- reads -------------------------------------------------------------
    def running(self, claim_prefix: str) -> list:
        out = []
        for board in self.boards:
            for task in kb.list_tasks(self.conn(board), status="running"):
                if (task.claim_lock or "").startswith(claim_prefix):
                    out.append(self._stamp(task, board))
        return out

    def ready(self, board: str, assignee: str) -> list:
        return [self._stamp(t, board) for t in kb.list_tasks(self.conn(board), assignee=assignee, status="ready")]

    def get(self, task_id: str):
        for board in self.boards:
            task = kb.get_task(self.conn(board), task_id)
            if task:
                return self._stamp(task, board)
        return None

    def comments(self, task_id: str) -> list:
        board = self._task_board.get(task_id) or self.boards[0]
        return kb.list_comments(self.conn(board), task_id)

    def counts(self, board: str) -> dict:
        rows = kb.list_tasks(self.conn(board), include_archived=False)
        out: dict[str, int] = {}
        for task in rows:
            out[task.status] = out.get(task.status, 0) + 1
        return out

    # -- writes ------------------------------------------------------------
    def comment(self, task_id: str, body: str, author: str) -> None:
        board = self._task_board.get(task_id) or self.boards[0]
        kb.add_comment(self.conn(board), task_id, author, body)

    def claim(self, task_id: str, claimer: str, ttl: int):
        board = self._task_board.get(task_id) or self.boards[0]
        task = kb.claim_task(self.conn(board), task_id, ttl_seconds=ttl, claimer=claimer)
        return self._stamp(task, board) if task else None

    def heartbeat(self, task_id: str, claimer: str, ttl: int) -> bool:
        board = self._task_board.get(task_id) or self.boards[0]
        return bool(kb.heartbeat_claim(self.conn(board), task_id, ttl_seconds=ttl, claimer=claimer))

    def reclaim(self, task_id: str, reason: str) -> bool:
        board = self._task_board.get(task_id) or self.boards[0]
        return bool(kb.reclaim_task(self.conn(board), task_id, reason=reason))

    def complete(self, task_id: str, summary: str, metadata: dict, run_id: Optional[int]) -> bool:
        board = self._task_board.get(task_id) or self.boards[0]
        try:
            return bool(kb.complete_task(self.conn(board), task_id, summary=summary,
                                         metadata=metadata, expected_run_id=run_id))
        except Exception:
            return False

    def block(self, task_id: str, reason: str, kind: str) -> bool:
        board = self._task_board.get(task_id) or self.boards[0]
        try:
            return bool(kb.block_task(self.conn(board), task_id, reason=reason, kind=kind))
        except Exception:
            return False
