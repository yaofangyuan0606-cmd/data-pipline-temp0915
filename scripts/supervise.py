#!/usr/bin/env python
"""守护脚本：服务进程退出（崩溃、被系统因内存不够杀掉、被 kill -9）就记一笔、推告警、过几秒自动拉起来。

    python scripts/supervise.py -- ./run.sh 8791
    setsid nohup .venv/bin/python scripts/supervise.py -- ./run.sh 8791 >> var/serve.log 2>&1 < /dev/null &

- 部署新代码：只杀服务进程（pkill -f "serve[.]py 8791"），它会正常退出（lifecycle 里记 stop），守护脚本看到是正常退出，
  不告警、马上用新代码重启。
- 彻底停掉：杀守护脚本本身（kill <守护脚本 pid>），它把信号转给服务进程、等它退出，然后自己也退出，不再重启。
- 异常退出记在 var/log/lifecycle.jsonl（event=exit，带退出码或信号）；10 分钟内重启超过 5 次就改成每分钟试一次，免得刷屏。
- 告警地址读 EMQC_ALERT_WEBHOOK（环境变量或项目根目录的 .env；写在 run.sh 里的这里看不到）。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emqc import logs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-restarts", type=int, default=0, help="最多重启几次后放弃（0 = 不限；测试用）")
    ap.add_argument("--delay", type=float, default=3.0, help="异常退出后等几秒再拉起")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后是启动服务的命令")
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("要给出启动命令，例如：-- ./run.sh 8791")

    stopping = False
    child: subprocess.Popen | None = None

    def forward(signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)

    env = dict(os.environ, EMQC_SUPERVISED="1")
    restarts: list[float] = []
    n = 0
    print(f"[supervise] pid {os.getpid()} 守护：{' '.join(cmd)}", flush=True)
    while True:
        child = subprocess.Popen(cmd, env=env, cwd=os.getcwd())
        rc = child.wait()
        if stopping:
            logs.lifecycle_event("supervisor_stop", child_pid=child.pid, code=rc)
            print(f"[supervise] 收到停止信号，服务已退出（code {rc}），守护结束", flush=True)
            return 0
        last = next(iter(logs.read_lifecycle(1)), None)
        planned = bool(last and last.get("event") == "stop" and last.get("pid") == child.pid)
        if planned:
            print(f"[supervise] 服务正常退出（部署重启），马上拉起", flush=True)
            delay = 0.5
        else:
            sig = -rc if rc < 0 else None
            why = f"被信号 {signal.Signals(sig).name} 杀掉" if sig else f"退出码 {rc}"
            logs.lifecycle_event("exit", child_pid=child.pid, code=rc, signal=sig,
                                 note=f"服务进程异常退出（{why}）" + ("；SIGKILL 多半是内存不够被系统杀掉，或被人 kill -9" if sig == 9 else ""))
            now = time.time()
            restarts = [t for t in restarts if now - t < 600] + [now]
            delay = 60.0 if len(restarts) > 5 else a.delay
            logs.alert("crash", f"服务进程异常退出（{why}），{int(delay)} 秒后自动重启；10 分钟内第 {len(restarts)} 次", wait=True)
            print(f"[supervise] 服务异常退出（{why}），{delay:.0f} 秒后重启", flush=True)
        n += 1
        if a.max_restarts and n > a.max_restarts:
            print("[supervise] 达到重启次数上限，守护结束", flush=True)
            return 1
        deadline = time.time() + delay
        while time.time() < deadline and not stopping:
            time.sleep(0.2)
        if stopping:
            logs.lifecycle_event("supervisor_stop", child_pid=child.pid, code=rc)
            return 0


if __name__ == "__main__":
    sys.exit(main())
