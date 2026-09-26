#!/bin/bash
# ChatGPT 节点守护 - 常驻循环(每分钟轻量体检, 异常才深度测试)
# keeper.py 内置 flock 进程锁 + 健康时静默退出, 高频运行零冲突零开销
DIR="$(cd "$(dirname "$0")" && pwd)"
while true; do
    /usr/bin/python3 "$DIR/keeper.py"
    sleep 55
done
