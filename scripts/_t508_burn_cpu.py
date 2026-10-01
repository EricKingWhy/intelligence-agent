"""#508 压力脚本的 CPU 负载源：N 个进程各占一核死循环，持续至多 seconds 秒。

用法: python scripts/_t508_burn_cpu.py [N] [seconds]
（N 默认 = os.cpu_count()；由 t508_exec_hardening_pressure.sh 调起，也可单独用。）

每个 _burn 进程自带与 seconds 相同的硬 TTL：父进程被强杀（SIGTERM /
TerminateProcess 都绕过 atexit，multiprocessing 的 daemon 清理不执行）时，
子进程最多活到请求的燃烧时长——孤儿存活上界 = 用户请求的负载时长，
正常收口由 t508_exec_hardening_pressure.sh 的 trap 杀树负责。
"""
import multiprocessing as mp
import os
import sys
import time


def _burn(stop, ttl: float) -> None:
    deadline = time.monotonic() + ttl
    while not stop.is_set() and time.monotonic() < deadline:
        pass


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else (os.cpu_count() or 4)
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0
    stop = mp.Event()
    procs = [mp.Process(target=_burn, args=(stop, seconds), daemon=True) for _ in range(n)]
    for p in procs:
        p.start()
    print(f"burning {n} cores for {seconds}s", flush=True)
    try:
        time.sleep(seconds)
    finally:
        stop.set()
        for p in procs:
            p.join()
