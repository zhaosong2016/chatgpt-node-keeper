#!/bin/bash
# ChatGPT 节点守护 - 手动兜底循环(仅在无法使用 launchd 时使用)
#
# 常规方式是 launchd(开机自启, 崩溃自动拉起):
#   加载: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.songsong.chatgpt-node-keeper*.plist
#   卸载: launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.songsong.chatgpt-node-keeper.plist
# 本脚本与 launchd 二选一即可, keeper.py 内置 flock 进程锁, 同时跑也不会冲突。
DIR="$(cd "$(dirname "$0")" && pwd)"
while true; do
    /usr/bin/python3 "$DIR/keeper.py"
    sleep 55
done
