#!/usr/bin/env python3
"""启动 PowerContext HTTP 服务（默认监听 127.0.0.1:8000，底层基于 OceanBase seekdb 持久化）。

用法：
    python dify/scripts/run_powercontext.py --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DIFY_DIR = Path(__file__).resolve().parents[1]
if str(DIFY_DIR) not in sys.path:
    sys.path.insert(0, str(DIFY_DIR))

from backend.app import EmbeddedSeekDBAndPowerContextRuntime, PowerContextHTTPServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=DIFY_DIR / ".runtime_data")
    args = parser.parse_args(argv)

    args.data_dir.mkdir(parents=True, exist_ok=True)
    embedded = EmbeddedSeekDBAndPowerContextRuntime(args.data_dir)
    embedded.start()
    server = PowerContextHTTPServer(embedded, host=args.host, port=args.port)
    server.start()
    print(f"PowerContext HTTP Server (SeekDB backend) listening on http://{args.host}:{args.port}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        embedded.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
