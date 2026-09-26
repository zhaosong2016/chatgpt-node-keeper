# ChatGPT 节点守护 (chatgpt-node-keeper)

双线路 ChatGPT 自动守护：主力线路每分钟体检，异常自动切换备用线路并在恢复后自动切回，配合本地仪表盘实时查看状态。适用于 macOS + Mihomo/Clash 系内核的代理环境。

## 架构

```
                        ┌─────────────────────┐
   每分钟 launchd 调度  │  keeper.py          │
   ───────────────────▶ │  轻量体检: 主线直测  │
                        │  ChatGPT 连通+延迟   │
                        └───────┬─────────────┘
                     健康 → 静默 │ 异常 → 决策
                                ▼
   ┌────────────────────────────────────────────────┐
   │ 主力: Mynet 自建(Clash Party, :7890)            │
   │   挂 → 切备用: SakuraCat 订阅(Vortex, :7897)   │
   │        └ 16 节点延迟初筛+逐个实测 ChatGPT 选优  │
   │   恢复 → 自动切回主力                            │
   └────────────────────────────────────────────────┘
                                │
                                ▼
              dashboard.py (:8788) 仪表盘自动刷新
              macOS 通知: 切换/故障/恢复 提醒(30min 节流)
```

系统代理由 keeper 统一接管与自愈(防睡眠唤醒后被抢占)，无需在客户端手动开关。

## 文件

| 文件 | 作用 |
|---|---|
| `keeper.py` | 守护核心：体检、深度测试、节点选优、双线切换、通知、状态输出 |
| `dashboard.py` | 本地仪表盘，浏览器打开 `http://127.0.0.1:8788` |
| `com.songsong.chatgpt-node-keeper.plist` | launchd: keeper 每 60s 调度 |
| `com.songsong.chatgpt-node-keeper.dashboard.plist` | launchd: 仪表盘常驻(崩溃自动拉起) |
| `keeper-loop.sh` | 手动兜底循环(无法使用 launchd 时) |

## 使用

```bash
# 1. 按需修改 keeper.py 顶部常量:
#    BASE(Vortex 控制 API) / MYNET_PROXY(主力端口) / PROXY(备用端口)
#    GROUP(代理分组名) / KEYWORDS(候选节点关键词)
#    MAIN_PORT / BACKUP_PORT(系统代理切换目标)

# 2. 安装 launchd 服务
cp com.songsong.chatgpt-node-keeper*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.songsong.chatgpt-node-keeper.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.songsong.chatgpt-node-keeper.dashboard.plist

# 3. 手动操作
/usr/bin/python3 keeper.py          # 跑一轮(自动判断轻重)
/usr/bin/python3 keeper.py --deep   # 强制深度测试(逐节点测速选优, 约1-2分钟)

# 4. 仪表盘
open http://127.0.0.1:8788
```

依赖: 仅 Python 3.9+ 标准库 + curl，零第三方依赖。

## 切换决策逻辑

| 主力状态 | 动作 |
|---|---|
| 可用且延迟达标 | 静默，零额外流量 |
| 异常(挂/慢/非200) | 备用线接管: 当前节点可用直接切, 否则 16 节点深度测试选优 |
| 备用节点全挂 | Top-N 全败则全量兜底扫描; 主备皆挂则通知告警 |
| 恢复健康 | 自动切回主力 |

节点选优带 25% 迟滞防抖(新节点不显著更快不切)、测速双通道(Cloudflare+CacheFly)互备、无效数据不做切换决策。开发过程详见 [DEVLOG.md](DEVLOG.md)。
