"""lanes_core — external agent lanes for the Hermes Kanban.

Pure standard library on purpose: the engine must be testable without Hermes,
without Paseo, and without a database. The dashboard's `plugin_api.py` is the
only Hermes-aware entry point.
"""

__all__ = ["config", "adapters", "engine", "kanban"]
