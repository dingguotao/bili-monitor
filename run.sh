#!/usr/bin/env bash
# B 站监控守护脚本
# 用法:  ./svc.sh {start|stop|restart|status|fg|logs}
#
# start   — 后台启动 scheduler.py (nohup), pid 写入 data/scheduler.pid
# stop    — 读 pid 文件, 发 SIGTERM, 等待优雅退出
# restart — stop + start
# status  — 打印 pid / 存活状态 / 最近 5 行日志
# fg      — 前台运行 (调试用, Ctrl-C 结束)
# logs    — tail -f data/scheduler.log

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/data/scheduler.pid"
LOG_FILE="$DIR/data/scheduler.log"
PY_BIN="${PY_BIN:-python3}"

mkdir -p "$DIR/data"

_pid_alive() {
    [ -f "$PID_FILE" ] || return 1
    local pid; pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    [ -z "$pid" ] && return 1
    kill -0 "$pid" 2>/dev/null
}

cmd_start() {
    if _pid_alive; then
        echo "已在运行, PID=$(cat "$PID_FILE")"
        return 0
    fi
    cd "$DIR" || exit 1
    nohup "$PY_BIN" scheduler.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 0.3
    if _pid_alive; then
        echo "已启动, PID=$(cat "$PID_FILE"), 日志: $LOG_FILE"
    else
        echo "启动失败, 查看日志: $LOG_FILE"
        return 1
    fi
}

cmd_stop() {
    if ! _pid_alive; then
        echo "未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid; pid=$(cat "$PID_FILE")
    echo "发送 SIGTERM 到 PID=$pid"
    kill -TERM "$pid" 2>/dev/null || true
    # 最多等 15s
    for _ in $(seq 1 30); do
        _pid_alive || break
        sleep 0.5
    done
    if _pid_alive; then
        echo "未响应, 发送 SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "已停止"
}

cmd_status() {
    if _pid_alive; then
        local pid; pid=$(cat "$PID_FILE")
        echo "运行中, PID=$pid"
        ps -p "$pid" -o pid,etime,rss,command 2>/dev/null | tail -n +1
    else
        echo "未运行"
    fi
    if [ -f "$LOG_FILE" ]; then
        echo "--- 日志末 5 行 ---"
        tail -n 5 "$LOG_FILE"
    fi
}

cmd_fg() {
    cd "$DIR" || exit 1
    exec "$PY_BIN" scheduler.py
}

cmd_logs() {
    tail -f "$LOG_FILE"
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status)  cmd_status ;;
    fg)      cmd_fg ;;
    logs)    cmd_logs ;;
    *)
        echo "用法: $0 {start|stop|restart|status|fg|logs}"
        exit 1
        ;;
esac
