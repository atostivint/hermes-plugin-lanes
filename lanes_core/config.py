"""Lane configuration: one JSON file, defaults filled in, never invented at runtime."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_PROVIDER = "codex/gpt-6-luna"


@dataclass
class Lane:
    """One (board, assignee) pair served by one adapter."""

    id: str
    adapter: str = "paseo"
    board: str = "default"
    assignee: str = "paseo"
    provider: str = DEFAULT_PROVIDER
    mode: str = "auto"
    # Where the adapter listens (e.g. your Paseo MCP URL). Empty = adapter default
    # or $PASEO_MCP_URL; never hard-coded so the repo carries no private host.
    adapter_url: str = ""
    max_concurrent: int = 1
    enabled: bool = True
    allowed_assignees: list[str] = field(default_factory=list)

    def claim_prefix(self) -> str:
        return f"lanes:{self.id}"


@dataclass
class Config:
    lanes: list[Lane] = field(default_factory=list)
    tick_seconds: int = 60
    auto_dispatch: bool = True
    brief_body_limit: int = 3000
    summary_limit: int = 1200

    def lane(self, lane_id: str) -> Optional[Lane]:
        for lane in self.lanes:
            if lane.id == lane_id:
                return lane
        return None


DEFAULT_CONFIG: dict[str, Any] = {
    "tick_seconds": 60,
    "auto_dispatch": True,
    "brief_body_limit": 3000,
    "summary_limit": 1200,
    "lanes": [
        {
            "id": "paseo-tronic",
            "adapter": "paseo",
            "board": "default",
            "assignee": "paseo",
            "provider": DEFAULT_PROVIDER,
            "mode": "auto",
            "max_concurrent": 1,
            "enabled": True,
        }
    ],
}


def config_path(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / "config.json"


def load(data_dir: str | os.PathLike) -> Config:
    """Read the config, writing the default one on first use (never silently)."""
    path = config_path(data_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    lanes = []
    for item in raw.get("lanes") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        known = {k: v for k, v in item.items() if k in Lane.__dataclass_fields__}
        lanes.append(Lane(**known))
    return Config(
        lanes=lanes,
        tick_seconds=int(raw.get("tick_seconds") or 60),
        auto_dispatch=bool(raw.get("auto_dispatch", True)),
        brief_body_limit=int(raw.get("brief_body_limit") or 3000),
        summary_limit=int(raw.get("summary_limit") or 1200),
    )


def save(data_dir: str | os.PathLike, cfg: Config) -> None:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tick_seconds": cfg.tick_seconds,
        "auto_dispatch": cfg.auto_dispatch,
        "brief_body_limit": cfg.brief_body_limit,
        "summary_limit": cfg.summary_limit,
        "lanes": [asdict(lane) for lane in cfg.lanes],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
