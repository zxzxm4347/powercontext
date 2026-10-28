#!/usr/bin/env python3
"""按拓扑顺序真实回放并验证 Dify Chatflow DSL (dsl/adventure_npc_chatflow.yml)。

用法：
    python dify/scripts/replay_chatflow.py
"""

from __future__ import annotations

import sys
from pathlib import Path

DIFY_DIR = Path(__file__).resolve().parents[1]
if str(DIFY_DIR) not in sys.path:
    sys.path.insert(0, str(DIFY_DIR))

from backend.app import run_verification_suite


if __name__ == "__main__":
    sys.exit(run_verification_suite())
