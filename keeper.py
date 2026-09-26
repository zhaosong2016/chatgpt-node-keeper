#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT 节点守护程序 v3 —— 高频检测, 稳定优先, 全链路容错

两级检测架构:
[每分钟] 轻量体检: 不切换节点、零额外流量, 只验证当前节点对
         ChatGPT 的连通性和响应速度。健康 → 什么都不做。
[按需]   深度测试: 仅当体检异常(节点挂了/变慢)或距上次深度测试
         超过 30 分钟时触发。逐个实测日/美节点的 ChatGPT 连通性
         + 带宽, 带迟滞切换(新节点没有快 25% 以上就不动)。

容错设计:
- flock 进程锁, 高频运行不撞车
- 单节点测试失败不影响其他节点(逐节点 try/except)
- 带宽测试双通道(Cloudflare 主 + CacheFly 备), 双失败时视为数据
  无效, 不做切换决策(避免垃圾数据引发误切)

手动运行: /usr/bin/python3 keeper.py          (自动判断轻重)
强制深度: /usr/bin/python3 keeper.py --deep
日志:     同目录 keeper.log (轻量体检正常时不写日志)
"""
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = "http://127.0.0.1:39797"    # Vortex 控制 API
PROXY = "http://127.0.0.1:7897"   # 本地混合代理端口
GROUP = "节点选择"
KEYWORDS = ("日本", "美国")        # ChatGPT 支持区, 绝不用香港
TOP_N = 6                          # 深度测试的节点数量
SPEED_BYTES = 10_000_000           # 带宽测试下载量(10MB)
HYSTERESIS = 1.25                  # 新节点要比当前快 25% 以上才切换
SPEED_FLOOR = 0.3                 # 低于此 MB/s 视为无效带宽数据
TRACE_TIMEOUT = 3.5               # 体检: 响应超过此秒数视为不健康
DEEP_INTERVAL = 1800              # 例行深度测试间隔(秒)

DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(DIR, "keeper.log")
LOCK = os.path.join(DIR, ".keeper.lock")
STATE = os.path.join(DIR, ".keeper.state")

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
            json.dump(d, f)
    except OSError:
        pass


# ---------------------------------------------------------------- 轻量体检
def light_check():
    """不切换任何节点, 只看当前节点对 ChatGPT 的健康度"""
    try:
        current = api("/proxies")["proxies"][GROUP].get("now", "")
    except Exception as e:
        return None, False, "Vortex API 不可达: %s" % e
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
        return
    current = data[GROUP].get("now", "")
    groups = {n for n, p in data.items() if p["type"] in ("Selector", "URLTest", "Fallback")}
    nodes = [n for n in data[GROUP]["all"]
             if n not in groups and any(k in n for k in KEYWORDS)]
    if not nodes:
        log("没有找到日/美节点, 请检查订阅")
        return

    # 延迟初筛(不切换节点)
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(delay_test, nodes))
    ok = sorted((r for r in results if r[1] < 9999), key=lambda x: x[1])
    if not ok:
        log("所有 %d 个日/美节点都超时, 请更新订阅" % len(nodes))
        return
    candidates = [n for n, _ in ok[:TOP_N]]
    log("候选 %d/%d, 逐一实测: %s" % (len(ok), len(nodes), " / ".join(candidates)))

    # 逐节点实测(单节点失败不影响其他)
    report = {}
    for node in candidates:
        try:
            if not switch(node):
                report[node] = (None, 0.0)
                continue
            time.sleep(0.5)
            loc = chatgpt_loc()
            mbps = speed_test() if loc else 0.0
            report[node] = (loc, mbps)
            log("  %s -> ChatGPT:%s, %.1f MB/s" % (node, loc or "不通", mbps))
        except Exception as e:
            report[node] = (None, 0.0)
            log("  %s -> 测试异常: %s" % (node, e))

    good = {n: v[1] for n, v in report.items() if v[0]}
    if not good:
        log("所有候选都无法访问 ChatGPT, 保持 %s" % current)
        switch(current)
        return

    best = max(good, key=good.get)          # 并列时取延迟最低(插入序)
    cur_loc, cur_speed = report.get(current, (None, 0.0))
    best_speed = good[best]

    # 决策: 数据无效不切; 数据有效时要求显著优势才切
    data_valid = best_speed >= SPEED_FLOOR
    new_better = data_valid and (cur_speed < SPEED_FLOOR
                                 or best_speed >= cur_speed * HYSTERESIS)

    if cur_loc and not new_better:
        if not data_valid:
            log("带宽数据无效(测速通道不可用), 保守保持 %s" % current)
        else:
            log("当前节点 %s 仍可用(%.1f MB/s), 最快候选优势不足 %.0f%%, 不切换"
                % (current, cur_speed, (HYSTERESIS - 1) * 100))
        switch(current)
        return

    switch(best)
    log(">>> 已切换: %s -> %s(%.1f MB/s, 出口 %s)" % (current, best, best_speed, report[best][0]))


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
    current = None
    try:
        state = load_state()
        current, healthy, detail = light_check()

        if force_deep:
            did_deep = True
            deep_test("手动强制")
        elif healthy:
            if time.time() - state.get("last_deep", 0) < DEEP_INTERVAL:
                return  # 一切正常, 静默退出(不写日志, 不刷新计时器)
            did_deep = True
            deep_test("例行巡检(距上次已超 %d 分钟)" % (DEEP_INTERVAL // 60))
        else:
            did_deep = True
            deep_test("体检异常: %s" % detail)
    except Exception as e:
        log("主流程异常(已兜住): %s" % e)
    finally:
        if did_deep:
            state = load_state()
            state["last_deep"] = time.time()
            state["last_node"] = current if current else None
            save_state(state)


if __name__ == "__main__":
    main()
