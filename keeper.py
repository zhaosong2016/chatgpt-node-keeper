#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT 节点守护程序 v4 —— 高频检测, 稳定优先, 全链路容错

两级检测架构:
[每分钟] 轻量体检: 不切换节点、零额外流量, 只验证当前节点对
         ChatGPT 的连通性和响应速度。健康 → 什么都不做。
[按需]   深度测试: 仅当体检异常(节点挂了/变慢)或距上次深度测试
         超过 30 分钟时触发。逐个实测日/美节点的 ChatGPT 连通性
         + 带宽, 带迟滞切换(新节点没有快 25% 以上就不动)。

v4 变更(相对 v3):
- 修复: 带宽数据无效(0.0 MB/s, 测速双通道全失败)时不再触发切换;
  区分三种情况 —— 当前可用且数据齐全 → 迟滞防抖; 当前可用但实测
  偏慢(< 阈值) → 切换更快候选; 当前挂了 → 按(有效数据 > 带宽 >
  延迟)降级选优, 并在日志中明确标注"降级选择"
- 修复: 分组名缺失(KeyError)不再被误报为 "Vortex API 不可达",
  体检结果区分 三态: True=健康 / False=节点异常 / None=守护链路异常
- 修复: 当前节点未进延迟 Top-N 时强制纳入实测, 不再被盲目切走
- 新增: macOS 通知中心告警(节点切换/全部候选不可用/API 异常),
  同类通知 30 分钟节流防骚扰
- 新增: 每次运行输出 status.json(当前节点/健康度/深度测试报告/
  切换历史), 供 dashboard.py 仪表盘展示
- 新增: 切换时若节点名地区与 ChatGPT 出口不符(中转落地)给出提示
- 新增: 全量兜底扫描 —— 延迟 Top-N 候选全部无法访问 ChatGPT 时
  (典型场景: OpenAI 按出口 IP 段封锁, 快节点整体沦陷), 自动扩大
  范围实测剩余全部日/美节点, 慢节点往往在不同 IP 段仍可用

v4.2 变更:
- 新增: 备用线路自动故障转移 —— Vortex 全部节点不可用时, 系统代理
  自动切到 Clash Party 备用线路(7890, Mynet); 主线路恢复健康后
  自动切回(7897)。系统代理由此程序统一管理, 请勿在 Vortex/
  Clash Party 里再手动操作系统代理开关, 避免互相打架。

容错设计:
- flock 进程锁, 高频运行不撞车(launchd 每分钟调度也安全)
- 单节点测试失败不影响其他节点(逐节点 try/except)
- 带宽测试双通道(Cloudflare 主 + CacheFly 备), 双失败时视为数据
  无效, 不做切换决策(避免垃圾数据引发误切)

手动运行: /usr/bin/python3 keeper.py          (自动判断轻重)
强制深度: /usr/bin/python3 keeper.py --deep
日志:     同目录 keeper.log (轻量体检正常时不写日志)
状态:     同目录 status.json (仪表盘数据源, 每次运行原子刷新)
常驻:     推荐 launchd(见同目录 *.plist), keeper-loop.sh 仅作手动兜底
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = "http://127.0.0.1:39797"    # Vortex 控制 API
PROXY = "http://127.0.0.1:7897"   # 本地混合代理端口(Vortex 主线路)
BACKUP_PROXY = "http://127.0.0.1:7890"  # Clash Party 备用线路(Mynet)
MAIN_PORT = 7897
BACKUP_PORT = 7890
GROUP = "节点选择"
KEYWORDS = ("日本", "美国")        # ChatGPT 支持区, 绝不用香港
TOP_N = 6                          # 深度测试的节点数量
SPEED_BYTES = 10_000_000           # 带宽测试下载量(10MB)
HYSTERESIS = 1.25                  # 新节点要比当前快 25% 以上才切换
SPEED_FLOOR = 0.3                 # 低于此 MB/s 视为无效带宽数据
TRACE_TIMEOUT = 3.5               # 体检: 响应超过此秒数视为不健康
DEEP_INTERVAL = 1800              # 例行深度测试间隔(秒)
NOTIFY_THROTTLE = 1800            # 同类通知最小间隔(秒), 防骚扰
HISTORY_MAX = 30                  # 仪表盘保留的切换历史条数

DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(DIR, "keeper.log")
LOCK = os.path.join(DIR, ".keeper.lock")
STATE = os.path.join(DIR, ".keeper.state")
STATUS = os.path.join(DIR, "status.json")

SPEED_URLS = (
    "https://speed.cloudflare.com/__down?bytes=%d" % SPEED_BYTES,
    "https://cachefly.cachefly.net/10mb.test",
)


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(kind, title, msg):
    """macOS 通知中心提醒, 同类(kind)通知在 NOTIFY_THROTTLE 内只发一次"""
    state = load_state()
    now = time.time()
    if now - state.get("notify_" + kind, 0) < NOTIFY_THROTTLE:
        return
    state["notify_" + kind] = now
    save_state(state)
    script = 'display notification "%s" with title "%s" sound name "Glass"' % (
        msg.replace('"', "'"), title.replace('"', "'"))
    try:
        subprocess.run(["osascript", "-e", script], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass  # 通知失败绝不影响主流程


def api(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers={"Content-Type": "application/json"})
    body = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
    if not body.strip():
        return {}  # PUT/DELETE 等操作成功但无返回体
    return json.loads(body)


def switch(node):
    """切换分组指向, 失败自动重试一次"""
    if not node:
        return False
    q = urllib.parse.quote(GROUP, safe="")
    for attempt in (1, 2):
        try:
            api("/proxies/%s" % q, "PUT", {"name": node})
            return True
        except Exception as e:
            if attempt == 2:
                log("  ! 切换到 %s 失败: %s" % (node, e))
                return False
            time.sleep(1)


def get_current():
    """读取分组当前实际指向的节点"""
    try:
        return api("/proxies")["proxies"][GROUP].get("now", "")
    except Exception:
        return None


# ------------------------------------------------- 系统代理与备用线路
def proxy_services():
    """返回当前启用了网页代理的网络服务名列表"""
    try:
        out = subprocess.run(["networksetup", "-listallnetworkservices"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    svcs = [l.strip() for l in out.splitlines()[1:]
            if l.strip() and not l.startswith("An asterisk")]
    enabled = []
    for s in svcs:
        try:
            info = subprocess.run(["networksetup", "-getwebproxy", s],
                                  capture_output=True, text=True, timeout=10).stdout
            if "Enabled: Yes" in info:
                enabled.append(s)
        except Exception:
            continue
    return enabled


def active_services():
    """返回当前有 IPv4 地址(联网中)的网络服务名列表"""
    try:
        out = subprocess.run(["networksetup", "-listallnetworkservices"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    svcs = [l.strip() for l in out.splitlines()[1:]
            if l.strip() and not l.startswith("An asterisk")]
    active = []
    for s in svcs:
        try:
            info = subprocess.run(["networksetup", "-getinfo", s],
                                  capture_output=True, text=True, timeout=10).stdout
            for line in info.splitlines():
                line = line.strip()
                if line.startswith("IP address:") and line != "IP address: none":
                    active.append(s)
                    break
        except Exception:
            continue
    return active


def set_system_proxy(port):
    """把系统 HTTP/HTTPS 代理指向 127.0.0.1:port。
    已有服务开代理则直接改; 全关时在联网服务上启用(带标准绕过列表)。"""
    svcs = proxy_services()
    fresh = not svcs
    if fresh:
        svcs = active_services()
    if not svcs:
        log("  ! 找不到可用网络服务, 无法切换系统代理到 %d" % port)
        return False
    ok = 0
    for s in svcs:
        for opt in ("-setwebproxy", "-setsecurewebproxy"):
            try:
                r = subprocess.run(["networksetup", opt, s, "127.0.0.1", str(port)],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    ok += 1
            except Exception:
                pass
        if fresh:
            try:
                subprocess.run(["networksetup", "-setproxybypassdomains", s,
                                "127.0.0.1", "192.168.0.0/16", "10.0.0.0/8",
                                "172.16.0.0/12", "localhost", "*.local", "<local>"],
                               capture_output=True, timeout=10)
            except Exception:
                pass
    if ok < len(svcs) * 2:
        log("  ! 系统代理切换到 %d 仅 %d/%d 项成功" % (port, ok, len(svcs) * 2))
    return ok >= len(svcs) * 2


def current_proxy_port():
    """读取系统 HTTP 代理端口; 代理全关返回 None"""
    try:
        out = subprocess.run(["scutil", "--proxy"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    if not re.search(r"HTTPEnable\s*:\s*1", out):
        return None
    m = re.search(r"HTTPPort\s*:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def backup_ok():
    """备用线路(Clash Party/Mynet)能否访问 ChatGPT"""
    out = curl(["-x", BACKUP_PROXY, "--max-time", "8", "-w",
                "\nMETRICS:%{http_code}", "https://chatgpt.com/cdn-cgi/trace"], timeout=12)
    return "METRICS:200" in out


def restore_main_if_failover(context):
    """处于备用线路状态且主线路已可用时, 把系统代理切回 Vortex(7897)"""
    st = load_state()
    if not st.get("failover"):
        return
    if set_system_proxy(MAIN_PORT):
        st["failover"] = False
        save_state(st)
        notify("recover", "节点守护: 主线路已恢复",
               "已切回 Vortex 主线路(7897), %s" % context)
        log(">>> 主线路恢复(%s), 系统代理切回 %d" % (context, MAIN_PORT))
    else:
        log("  ! 系统代理切回 %d 失败, 继续使用备用线路" % MAIN_PORT)


def delay_test(name):
    """通过 API 直测单节点延迟, 不需要切换分组"""
    q = urllib.parse.quote(name, safe="")
    try:
        r = api("/proxies/%s/delay?timeout=3000&url=http://www.gstatic.com/generate_204" % q)
        return name, r.get("delay", 9999)
    except Exception:
        return name, 9999


def curl(args, timeout=25):
    try:
        return subprocess.run(["curl", "-s", *args], capture_output=True,
                              text=True, timeout=timeout).stdout
    except Exception:
        return ""


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    try:
        with open(STATE, "w") as f:
            json.dump(d, f, ensure_ascii=False)
    except OSError:
        pass


def write_status(current, healthy, detail, state):
    """每次运行后原子刷新 status.json, 供仪表盘读取"""
    last_deep = state.get("last_deep", 0)
    try:
        last_deep_h = (datetime.fromtimestamp(last_deep)
                       .strftime("%Y-%m-%d %H:%M:%S")) if last_deep else ""
    except (ValueError, OSError):
        last_deep_h = ""
    status = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
        "current": current,
        "healthy": healthy,   # True/False/None(守护链路异常)
        "detail": detail,
        "failover": bool(state.get("failover")),
        "last_deep": last_deep_h,
        "history": state.get("history", []),
        "last_report": state.get("last_report", {}),
    }
    try:
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, ensure_ascii=False)
        os.replace(tmp, STATUS)
    except OSError:
        pass


# ---------------------------------------------------------------- 轻量体检
def light_check():
    """
    返回 (当前节点, 健康度, 详情):
      healthy=True  → 节点健康
      healthy=False → 节点异常(挂了/变慢/非 200)
      healthy=None  → 守护链路异常(API 不可达/分组缺失), 不是节点问题
    """
    try:
        proxies = api("/proxies")["proxies"]
    except Exception as e:
        return None, None, "Vortex API 不可达: %s" % e
    if GROUP not in proxies:
        return None, None, "代理列表中不存在分组「%s」(请检查订阅或分组名)" % GROUP
    current = proxies[GROUP].get("now", "")
    out = curl(["-x", PROXY, "--max-time", "8", "-w",
                "\nMETRICS:%{http_code}:%{time_total}",
                "https://chatgpt.com/cdn-cgi/trace"], timeout=12)
    if "METRICS:" not in out:
        return current, False, "ChatGPT 无响应(节点疑似挂掉)"
    code, _, t = out.rsplit("METRICS:", 1)[1].strip().partition(":")
    try:
        elapsed = float(t)
    except ValueError:
        elapsed = 99.0
    if code != "200":
        return current, False, "ChatGPT 返回 HTTP %s" % code
    if elapsed > TRACE_TIMEOUT:
        return current, False, "响应过慢(%.1fs > %.1fs)" % (elapsed, TRACE_TIMEOUT)
    return current, True, "ok %.1fs" % elapsed


# ---------------------------------------------------------------- 深度测试
def chatgpt_loc():
    out = curl(["-x", PROXY, "--max-time", "8", "https://chatgpt.com/cdn-cgi/trace"], timeout=12)
    for line in out.splitlines():
        if line.startswith("loc="):
            return line.split("=", 1)[1]
    return None


def speed_test():
    """双通道带宽测试, 都失败返回 0.0(视为无效数据)"""
    for url in SPEED_URLS:
        out = curl(["-x", PROXY, "-o", "/dev/null", "-w", "%{speed_download}",
                    "--max-time", "15", url], timeout=20)
        try:
            mbps = float(out) / 1024 / 1024
        except (ValueError, TypeError):
            continue
        if mbps > 0:
            return mbps
    return 0.0


def deep_test(reason):
    log(">>> 深度测试启动(原因: %s)" % reason)
    try:
        data = api("/proxies")["proxies"]
    except Exception as e:
        log("无法连接 Vortex API(%s), 放弃本轮" % e)
        notify("api", "节点守护: Vortex API 不可达", "深度测试被迫放弃: %s" % e)
        return
    if GROUP not in data:
        log("代理列表中不存在分组「%s」, 放弃本轮" % GROUP)
        notify("group", "节点守护: 分组缺失",
               "代理列表中不存在分组「%s」, 请检查订阅" % GROUP)
        return
    current = data[GROUP].get("now", "")
    groups = {n for n, p in data.items() if p["type"] in ("Selector", "URLTest", "Fallback")}
    nodes = [n for n in data[GROUP]["all"]
             if n not in groups and any(k in n for k in KEYWORDS)]
    if not nodes:
        log("没有找到日/美节点, 请更新订阅")
        notify("nosub", "节点守护: 无可用日/美节点",
               "订阅中没有日本/美国节点, 请更新订阅")
        return

    # 延迟初筛(不切换节点)
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(delay_test, nodes))
    ok = sorted((r for r in results if r[1] < 9999), key=lambda x: x[1])
    if not ok:
        log("所有 %d 个日/美节点都超时, 请更新订阅" % len(nodes))
        notify("nosub", "节点守护: 日/美节点全部超时",
               "全部 %d 个节点延迟测试超时, 请更新订阅" % len(nodes))
        return
    candidates = [n for n, _ in ok[:TOP_N]]
    # 当前节点即使没进延迟 Top-N 也强制纳入实测, 避免因"没测到"被盲目切走
    if current and current in nodes and current not in candidates:
        candidates.append(current)
    log("候选 %d/%d, 逐一实测: %s" % (len(ok), len(nodes), " / ".join(candidates)))

    # 逐节点实测(单节点失败不影响其他)
    report = {}

    def measure(node):
        try:
            if not switch(node):
                report[node] = (None, 0.0)
                return
            time.sleep(0.5)
            loc = chatgpt_loc()
            mbps = speed_test() if loc else 0.0
            report[node] = (loc, mbps)
            log("  %s -> ChatGPT:%s, %.1f MB/s" % (node, loc or "不通", mbps))
        except Exception as e:
            report[node] = (None, 0.0)
            log("  %s -> 测试异常: %s" % (node, e))

    for node in candidates:
        measure(node)

    # 兜底: Top-N 候选全部无法访问 ChatGPT 时, 扩大到剩余全部日/美节点。
    # 场景: OpenAI 按 IP 段封锁时, 快节点常整体沦陷, 慢节点反而可用。
    if not any(report.get(n, (None, 0.0))[0] for n in candidates):
        rest = [n for n in nodes if n not in report]
        if rest:
            log("候选全部失败, 扩大范围实测剩余 %d 个节点: %s"
                % (len(rest), " / ".join(rest)))
            for node in rest:
                measure(node)

    delay_map = dict(ok)
    delay_rank = {n: i for i, (n, _) in enumerate(ok)}

    # 深度测试结果先落盘(无论后续是否切换, 供仪表盘展示)
    st = load_state()
    st["last_report"] = {n: {"loc": report[n][0],
                             "speed": round(report[n][1], 2),
                             "delay": delay_map.get(n)}
                         for n in report}
    st["last_deep_run"] = time.time()
    save_state(st)

    good = [n for n in report if report[n][0]]
    if not good:
        log("全部 %d 个日/美节点都无法访问 ChatGPT(疑似出口 IP 段被 OpenAI 封锁), 保持 %s"
            % (len(report), current))
        switch(current)
        # 主线路全挂: 自动故障转移到 Clash Party 备用线路
        if backup_ok():
            if set_system_proxy(BACKUP_PORT):
                st = load_state()
                if not st.get("failover"):
                    st["failover"] = True
                    save_state(st)
                    notify("failover", "节点守护: 已切换备用线路",
                           "Vortex 全部节点不可用, 系统代理已切到 Mynet 备用线(7890)")
                log(">>> 主线路全挂, 系统代理已切到备用线路 Mynet(%d)" % BACKUP_PORT)
        else:
            notify("alldown", "节点守护: 主/备线路均不可用",
                   "Vortex 全部节点与 Mynet 备用线都无法访问 ChatGPT")
        return

    def rank_key(n):
        """降级选优排序: 有效带宽数据 > 带宽高低 > 延迟高低"""
        speed = report[n][1]
        return (1 if speed >= SPEED_FLOOR else 0, speed, -delay_rank.get(n, 9999))

    best = max(good, key=rank_key)
    best_speed = report[best][1]
    best_valid = best_speed >= SPEED_FLOOR
    cur_loc, cur_speed = report.get(current, (None, 0.0))
    cur_valid = cur_loc is not None and cur_speed >= SPEED_FLOOR

    if best == current:
        log("当前节点 %s 已是候选中最优(%.1f MB/s), 不切换" % (current, best_speed))
        switch(current)
        restore_main_if_failover("节点 %s 可用" % best)
        return

    if cur_valid:
        # 数据齐全: 要求新节点快 25% 以上才切(迟滞防抖)
        if not (best_valid and best_speed >= cur_speed * HYSTERESIS):
            log("当前节点 %s 仍可用(%.1f MB/s), 最快候选 %s(%.1f MB/s)优势不足 %.0f%%, 不切换"
                % (current, cur_speed, best, best_speed, (HYSTERESIS - 1) * 100))
            switch(current)
            restore_main_if_failover("节点 %s 可用(%.1f MB/s)" % (current, cur_speed))
            return
    elif cur_loc and cur_speed > 0:
        # 当前节点可用但实测带宽低于可用阈值(慢节点, 非测速失败):
        # 候选有有效数据即切换, 无需迟滞
        if not best_valid:
            log("当前节点 %s 带宽仅 %.1f MB/s, 但候选均无有效带宽数据, 保守保持"
                % (current, cur_speed))
            switch(current)
            restore_main_if_failover("节点 %s 可用" % current)
            return
        log("当前节点 %s 带宽仅 %.1f MB/s(低于 %.1f 阈值), 切换到更快的 %s"
            % (current, cur_speed, SPEED_FLOOR, best))
    elif cur_loc:
        # 当前节点可用但测速双通道全失败(0.0): 无效数据不做切换决策
        log("当前节点 %s 可用但带宽数据无效(测速通道不可用), 保守保持不切换" % current)
        switch(current)
        restore_main_if_failover("节点 %s 可用" % current)
        return
    else:
        # 当前节点无法访问 ChatGPT: 降级选优(优先有效数据, 其次延迟最低)
        log("当前节点 %s 无法访问 ChatGPT, 从 %d 个可用候选中选优"
            % (current, len(good)))

    if not switch(best):
        switch(current)  # 切换失败至少恢复原状
        return
    log(">>> 已切换: %s -> %s(%.1f MB/s, 出口 %s)"
        % (current, best, best_speed, report[best][0]))
    if not best_valid:
        log("  ! 新节点带宽数据无效(测速通道不可用), 本次为降级选择, 下轮体检会复核")
    region = "日本" if "日本" in best else ("美国" if "美国" in best else None)
    if region and report[best][0] not in ("JP", "US"):
        log("  ! 注意: 节点名含「%s」但 ChatGPT 出口为 %s(中转落地), 地区与出口不符"
            % (region, report[best][0]))
    notify("switch", "节点守护: 已切换节点",
           "%s → %s(%.1f MB/s, 出口 %s)" % (current, best, best_speed, report[best][0]))
    restore_main_if_failover("已切换到 %s(%.1f MB/s)" % (best, best_speed))

    st = load_state()
    hist = st.setdefault("history", [])
    hist.insert(0, {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "from": current, "to": best,
                    "speed": round(best_speed, 1), "loc": report[best][0]})
    del hist[HISTORY_MAX:]
    save_state(st)


# ---------------------------------------------------------------- 主流程
def main():
    force_deep = "--deep" in sys.argv

    # 进程锁(flock): 有实例在跑就静默退出
    lockfile = open(LOCK, "w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return

    did_deep = False
    current, healthy, detail = None, None, "未运行"
    try:
        state = load_state()
        current, healthy, detail = light_check()

        # 系统代理自愈: 代理开着但指向不对时纠偏(防睡眠唤醒/网络切换后
        # 被其他代理应用抢占或残留)。用户主动全关代理则尊重不强开;
        # 主线路不健康时不纠偏, 交给深度测试决策(可能要切备用线)。
        failover = bool(state.get("failover"))
        port = current_proxy_port()
        if port is not None:
            expected = BACKUP_PORT if failover else (MAIN_PORT if healthy else None)
            if expected and port != expected and set_system_proxy(expected):
                log("系统代理 %d -> %d(自愈纠偏)" % (port, expected))

        if failover:
            # 备用线路期间: 主线路恢复健康立即切回;
            # 未恢复则按 DEEP_INTERVAL 节奏巡检主线路, 不每分钟折腾
            if healthy:
                restore_main_if_failover("当前节点 %s(%s)" % (current, detail))
                return
            if force_deep or time.time() - state.get("last_deep", 0) >= DEEP_INTERVAL:
                did_deep = True
                deep_test("备用期间主线路巡检: %s" % detail)
            # 其余情况: 保持备用线路, 静默等待下轮体检
        elif force_deep:
            did_deep = True
            deep_test("手动强制")
        elif healthy:
            if time.time() - state.get("last_deep", 0) < DEEP_INTERVAL:
                return  # 一切正常, 静默退出(不写日志)
            did_deep = True
            deep_test("例行巡检(距上次已超 %d 分钟)" % (DEEP_INTERVAL // 60))
        else:
            did_deep = True
            deep_test("体检异常: %s" % detail)
    except Exception as e:
        log("主流程异常(已兜住): %s" % e)
    finally:
        state = load_state()
        if did_deep:
            state["last_deep"] = time.time()
            state["last_node"] = current or None
            # 深度测试可能已切换节点, 重新读取实际指向, 保证 status.json 准确
            fresh = get_current()
            if fresh:
                current = fresh
        save_state(state)
        write_status(current, healthy, detail, state)


if __name__ == "__main__":
    main()
