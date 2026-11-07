#!/usr/bin/env python3
"""Stdlib runner for tests/test_engine.py (no pytest needed on the target host).

pytest works too — the test module is plain functions with asserts — but the
Hermes host does not always have pytest, and a plugin that can only be tested
after installing a test framework is a plugin nobody tests.
"""

import importlib.util
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("test_engine", ROOT / "tests" / "test_engine.py")
module = importlib.util.module_from_spec(spec)
sys.modules["test_engine"] = module  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(module)

tests = sorted(name for name in dir(module) if name.startswith("test_"))
failed = 0
for name in tests:
    try:
        getattr(module, name)()
    except Exception:
        failed += 1
        print(f"FAIL {name}")
        traceback.print_exc(limit=3)
    else:
        print(f"ok   {name}")

print(f"\n{len(tests) - failed}/{len(tests)} passed")
sys.exit(1 if failed else 0)
