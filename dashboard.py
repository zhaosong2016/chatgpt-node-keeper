#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT 节点守护 - 本地状态仪表盘

浏览器打开: http://127.0.0.1:8788
数据源:     同目录 status.json(keeper.py 每次运行刷新) + keeper.log 尾部
仅监听 127.0.0.1, 不对外暴露; 零第三方依赖(Python 3.9 标准库)。

手动运行: /usr/bin/python3 dashboard.py
常驻:     launchd 加载 com.chatgpt.keeper.dashboard.plist(推荐)
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(DIR, "status.json")
LOG = os.path.join(DIR, "keeper.log")
PORT = 8788

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChatGPT 节点守护</title>
<style>
:root {
  --bg: #101418; --card: #1a2027; --card2: #212932;
  --text: #e8edf2; --muted: #8b98a5; --line: #2c3742;
  --ok: #34c77b; --bad: #e5534b; --warn: #e3a008; --accent: #4c8dff;
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--bg); color: var(--text);
  font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  padding: 24px;
}
.wrap { max-width: 960px; margin: 0 auto; }
header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
h1 { font-size: 18px; font-weight: 600; }
#fresh { color: var(--muted); font-size: 12px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
}
.card h2 {
  font-size: 12px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .08em; margin-bottom: 10px;
}
.wide { grid-column: 1 / -1; }
.node { font-size: 22px; font-weight: 600; word-break: break-all; }
.badge {
  display: inline-block; padding: 2px 10px; border-radius: 99px;
  font-size: 12px; font-weight: 600; margin-left: 10px; vertical-align: 3px;
}
.badge.ok   { background: rgba(52,199,123,.15); color: var(--ok); }
.badge.bad  { background: rgba(229,83,75,.15);  color: var(--bad); }
.badge.warn { background: rgba(227,160,8,.15);  color: var(--warn); }
.kv { color: var(--muted); font-size: 13px; margin-top: 8px; }
.kv b { color: var(--text); font-weight: 500; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
td.num { font-variant-numeric: tabular-nums; }
.bar { position: relative; background: var(--card2); border-radius: 4px; height: 8px; min-width: 90px; }
.bar i { position: absolute; inset: 0 auto 0 0; background: var(--accent); border-radius: 4px; }
.dim { color: var(--muted); }
.tag { padding: 1px 7px; border-radius: 4px; background: var(--card2); font-size: 12px; }
.tag.us { color: var(--warn); } .tag.jp { color: var(--ok); } .tag.no { color: var(--bad); }
pre {
  background: var(--card2); border-radius: 8px; padding: 12px;
  font: 12px/1.7 "SF Mono", Menlo, Consolas, monospace;
  overflow-x: auto; white-space: pre-wrap; word-break: break-all;
  max-height: 320px; overflow-y: auto; color: #aebac6;
}
footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 18px; }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>ChatGPT 节点守护</h1>
    <span id="fresh">加载中…</span>
  </header>

  <div class="grid">
    <div class="card">
      <h2>当前节点</h2>
      <div><span class="node" id="node">—</span><span id="badge"></span></div>
      <div class="kv" id="detail">—</div>
      <div class="kv">上次深度测试: <b id="deep">—</b></div>
    </div>

    <div class="card">
      <h2>最近深度测试</h2>
      <table>
        <thead><tr><th>节点</th><th>出口</th><th style="width:120px">带宽</th></tr></thead>
        <tbody id="report"><tr><td colspan="3" class="dim">暂无数据</td></tr></tbody>
      </table>
    </div>

    <div class="card wide">
      <h2>切换历史</h2>
      <table>
        <thead><tr><th>时间</th><th>从</th><th>到</th><th>带宽</th><th>出口</th></tr></thead>
        <tbody id="history"><tr><td colspan="5" class="dim">暂无切换记录</td></tr></tbody>
      </table>
    </div>

    <div class="card wide">
      <h2>运行日志(尾部 200 行)</h2>
      <pre id="log">加载中…</pre>
    </div>
  </div>

  <footer>keeper.py 每分钟体检 · 每 30 分钟例行深度测试 · 数据每 5 秒自动刷新</footer>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function ago(ts) {
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 90) return s + " 秒前更新";
  if (s < 5400) return Math.round(s/60) + " 分钟前更新";
  return Math.round(s/3600) + " 小时前更新";
}

function badgeOf(d) {
  if (d.failover)         return '<span class="badge warn">备用线路中</span>';
  if (d.healthy === true)  return '<span class="badge ok">健康</span>';
  if (d.healthy === false) return '<span class="badge bad">异常</span>';
  return '<span class="badge warn">守护未连通</span>';
}

function locTag(loc) {
  if (!loc) return '<span class="tag no">不通</span>';
  if (loc === "US") return '<span class="tag us">US</span>';
  if (loc === "JP") return '<span class="tag jp">JP</span>';
  return '<span class="tag">' + esc(loc) + "</span>";
}

function speedBar(v) {
  if (!v) return '<span class="dim">无效数据</span>';
  const w = Math.min(100, Math.round(v / 5 * 100));
  return '<div class="bar"><i style="width:' + w + '%"></i></div>';
}

async function refresh() {
  try {
    const r = await fetch("/status.json", {cache: "no-store"});
    const d = await r.json();
    if (d.error) {
      $("fresh").textContent = "等待 keeper 首次运行…";
      $("node").textContent = "—"; $("badge").innerHTML = badgeOf({});
      $("detail").textContent = d.error; return;
    }
    $("fresh").textContent = ago(d.updated_ts);
    $("node").textContent = d.current || "(未知)";
    $("badge").innerHTML = badgeOf(d);
    $("detail").innerHTML = "体检详情: <b>" + esc(d.detail) + "</b>";
    if (d.failover)
      $("detail").innerHTML += "<br>系统流量当前走 <b>Mynet 备用线(7890)</b>, 主线路恢复后自动切回";
    $("deep").textContent = d.last_deep || "从未";

    const rep = Object.entries(d.last_report || {})
      .sort((a, b) => (b[1].speed || 0) - (a[1].speed || 0));
    $("report").innerHTML = rep.length ? rep.map(([n, v]) =>
      "<tr><td>" + esc(n) + "</td><td>" + locTag(v.loc) +
      "</td><td>" + (v.speed ? '<span class="num">' + v.speed + " MB/s</span> " : "") +
      speedBar(v.speed) + "</td></tr>").join("")
      : '<tr><td colspan="3" class="dim">暂无数据</td></tr>';

    const hist = d.history || [];
    $("history").innerHTML = hist.length ? hist.map(h =>
      "<tr><td class='num dim'>" + esc(h.time) + "</td><td>" + esc(h.from || "—") +
      "</td><td><b>" + esc(h.to) + "</b></td><td class='num'>" +
      (h.speed ? h.speed + " MB/s" : "—") + "</td><td>" + locTag(h.loc) + "</td></tr>").join("")
      : '<tr><td colspan="5" class="dim">暂无切换记录</td></tr>';
  } catch (e) {
    $("fresh").textContent = "仪表盘服务连接失败";
  }
  try {
    const r = await fetch("/log", {cache: "no-store"});
    $("log").textContent = await r.text() || "(暂无日志)";
  } catch (e) { $("log").textContent = "(日志读取失败)"; }
}

refresh();
setInterval(refresh, 5000);
setInterval(() => {
  const f = $("fresh");
  if (f.textContent.includes("前更新")) refresh();
}, 15000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        elif self.path == "/status.json":
            try:
                with open(STATUS, "rb") as f:
                    body = f.read()
            except OSError:
                body = ('{"error": "尚无状态数据, 等待 keeper 首次运行"}').encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path == "/log":
            try:
                with open(LOG, "rb") as f:
                    lines = f.read().decode("utf-8", "replace").splitlines()
                body = "\n".join(lines[-200:]).encode("utf-8")
            except OSError:
                body = "(暂无日志)".encode("utf-8")
            ctype = "text/plain; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 不往 stderr 刷访问日志


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("ChatGPT 节点守护仪表盘: http://127.0.0.1:%d" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
