#!/usr/bin/env python3
"""重置本地运行目录中的玩家测试数据，恢复至初始种子状态。

用法：
    python dify/scripts/reset_player.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

DIFY_DIR = Path(__file__).resolve().parents[1]


def main() -> int:
    runtime_dir = DIFY_DIR / ".runtime_data"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
        print(f"Removed runtime data directory: {runtime_dir}")
    else:
        print("Runtime data directory is already clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
