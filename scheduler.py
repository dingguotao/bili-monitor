#!/usr/bin/env python3
"""B 站动态监控 - 自动调度守护进程

规则:
    工作日 09:00-14:59 → 每 2 分钟跑一次 monitor.py
    其它时间           → 每 60 分钟跑一次 monitor.py

行为:
    - 阻塞循环, 收到 SIGTERM/SIGINT 立即结束
    - 每次先跑 monitor.py, 结束后再根据"当前时间"决定下次 sleep 时长
    - 单次跑动的 stdout/stderr 追加到 data/scheduler.log
    - 打印 PID / 命令 / 耗时到日志, 便于事后排查
"""

import datetime
import os
import signal
import subprocess
import sys
import time

from config_loader import config

_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(_DIR, "monitor.py")
LOG_FILE = config.get_path("scheduler.log_file")

HIGH_FREQ_SEC   = config.get("scheduler.high_freq_interval_seconds", 120)
LOW_FREQ_SEC    = config.get("scheduler.low_freq_interval_seconds", 3600)
HIGH_FREQ_START_H = config.get("scheduler.high_freq_start_hour", 9)
HIGH_FREQ_END_H   = config.get("scheduler.high_freq_end_hour", 15)
_MONITOR_TIMEOUT  = config.get("scheduler.monitor_run_timeout_seconds", 180)
_HEARTBEAT        = config.get("scheduler.shutdown_check_interval_seconds", 5)

_running = True


def _handle_signal(signum, _frame):
    global _running
    _running = False
    _log(f"收到信号 {signum}, 循环结束")


def _log(msg):
    """写日志文件；若 stderr 未被重定向到 LOG_FILE（前台运行）则同时打屏。"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [scheduler] {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    # 后台运行时 stderr 被 svc.sh 重定向到 LOG_FILE, 避免重复
    if sys.stderr.isatty():
        sys.stderr.write(line)


def is_high_freq_now():
    """工作日 09:00-14:59 返回 True。"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:              # 5=Sat, 6=Sun
        return False
    return HIGH_FREQ_START_H <= now.hour < HIGH_FREQ_END_H


def next_interval_sec():
    return HIGH_FREQ_SEC if is_high_freq_now() else LOW_FREQ_SEC


def run_once():
    """跑一次 monitor.py, 记录耗时和退出码。"""
    start = time.time()
    _log(f"→ 执行 monitor.py (interval_after={next_interval_sec()}s)")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            r = subprocess.run(
                [sys.executable, MONITOR],
                stdout=f, stderr=subprocess.STDOUT,
                cwd=_DIR, timeout=_MONITOR_TIMEOUT,
            )
        dur = time.time() - start
        _log(f"← monitor.py 退出码 {r.returncode}, 耗时 {dur:.1f}s")
    except subprocess.TimeoutExpired:
        _log(f"✗ monitor.py 超时 {_MONITOR_TIMEOUT}s, 已终止")
    except Exception as e:
        _log(f"✗ monitor.py 异常: {e}")


def _sleep_interruptible(total):
    """把长 sleep 切碎, 让 SIGTERM 尽快生效。"""
    step = _HEARTBEAT
    remain = total
    while _running and remain > 0:
        time.sleep(min(step, remain))
        remain -= step


def main():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    _log(f"守护进程启动, PID={os.getpid()}, monitor={MONITOR}")

    while _running:
        run_once()
        if not _running:
            break
        wait = next_interval_sec()
        mode = "HIGH" if wait == HIGH_FREQ_SEC else "LOW"
        _log(f"睡眠 {wait}s ({mode})")
        _sleep_interruptible(wait)

    _log("守护进程退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
