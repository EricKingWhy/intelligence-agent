"""真实 kill 用的子进程（#1489 验收 2）。

逐条 `append_async` 提交事件，把**每条确认成功的 seq** 逐行打到 stdout 并 flush，
全部提交完后 `os._exit(137)`——不跑清理、不给缓冲机会。

父进程据此核对：已确认的每一条在 kill+重启后都必须可读，且 seq 保序无重复。

argv[1] = 沙盘根目录（`sessions/` 建在其下）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session

ROUNDS = 8


async def main() -> None:
    root = Path(sys.argv[1])
    store = JsonlSessionStore(root / "sessions")
    session = Session.start(store)
    print(session.session_id, flush=True)

    confirmed: list[int] = []
    for index in range(ROUNDS):
        event = await session.append_async(USER_MESSAGE, {"content": f"batch-{index}"})
        confirmed.append(event.seq)
        # 逐条 flush：每条都代表"调用方已看到成功"，崩溃前必须已成立。
        print(f"confirmed {event.seq}", flush=True)

    sys.stdout.write("#END\n" + json.dumps(confirmed) + "\n")
    sys.stdout.flush()
    os._exit(137)  # 真实崩溃：不跑 atexit、不 flush 任何遗留缓冲


if __name__ == "__main__":
    asyncio.run(main())
