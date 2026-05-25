#!/usr/bin/env python3
"""Authenticated mobile web wrapper for the local Hyperliquid order helper."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import ssl
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
TOKEN = os.environ.get("SIMPLE_HYPER_TOKEN", "")
TLS_CERT = os.environ.get("SIMPLE_HYPER_TLS_CERT", "")
TLS_KEY = os.environ.get("SIMPLE_HYPER_TLS_KEY", "")
DEFAULT_AMOUNT = os.environ.get("SIMPLE_HYPER_DEFAULT_AMOUNT", "10")
DEFAULT_COINS = [
    item.strip()
    for item in os.environ.get("SIMPLE_HYPER_COINS", "BTC,ETH,HYPE,GOLD,SAMSUNG,QQQ").split(",")
    if item.strip()
]
MAX_AMOUNT_USD = Decimal(os.environ.get("SIMPLE_HYPER_MAX_AMOUNT_USD", "1000"))
COMMAND_TIMEOUT = float(os.environ.get("SIMPLE_HYPER_COMMAND_TIMEOUT", "60"))
COIN_RE = re.compile(r"^[A-Za-z0-9:_./-]{1,48}$")
OID_RE = re.compile(r"^[0-9]{1,30}$")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#f7f4ef">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Simple-Hyper">
  <title>Simple-Hyper</title>
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" href="/icon.svg" type="image/svg+xml">
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f4ef;
      --ink: #141414;
      --muted: #6b6259;
      --line: #d9d0c5;
      --panel: #fffdf8;
      --buy: #137547;
      --sell: #b3261e;
      --accent: #245f73;
      --warn: #8a5a00;
      --shadow: 0 10px 30px rgba(42, 34, 27, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      min-height: 100%;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    body {
      padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
    }

    button,
    input,
    select {
      font: inherit;
    }

    .shell {
      width: min(100%, 760px);
      margin: 0 auto;
      padding: 18px 14px 32px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }

    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.1;
      font-weight: 750;
    }

    .status {
      min-width: 72px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      text-align: center;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.54);
      font-size: 13px;
      white-space: nowrap;
    }

    .status.ready {
      color: var(--buy);
      border-color: rgba(19, 117, 71, 0.26);
      background: rgba(19, 117, 71, 0.08);
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
      margin: 12px 0;
    }

    .toolbar {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 10px;
    }

    .row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 10px;
    }

    .field {
      display: grid;
      gap: 6px;
      min-width: 0;
    }

    label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    input,
    select {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px 11px;
      background: #fff;
      color: var(--ink);
      outline: none;
    }

    input:focus,
    select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(36, 95, 115, 0.14);
    }

    .segmented {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f1ece4;
    }

    .segmented button {
      min-height: 38px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      font-weight: 700;
    }

    .segmented button.active.buy {
      color: #fff;
      background: var(--buy);
    }

    .segmented button.active.sell {
      color: #fff;
      background: var(--sell);
    }

    .toggle {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      background: #fff;
    }

    .toggle input {
      width: 22px;
      min-height: 22px;
      accent-color: var(--accent);
    }

    .button {
      min-height: 46px;
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 10px 12px;
      background: #fff;
      color: var(--ink);
      font-weight: 760;
      white-space: nowrap;
    }

    .button:active {
      transform: translateY(1px);
    }

    .button.primary {
      background: var(--accent);
      color: #fff;
    }

    .button.buy {
      background: var(--buy);
      color: #fff;
    }

    .button.sell,
    .button.danger {
      background: var(--sell);
      color: #fff;
    }

    .button.ghost {
      border-color: var(--line);
      background: rgba(255, 255, 255, 0.68);
      color: var(--accent);
    }

    .button:disabled {
      opacity: 0.5;
      transform: none;
    }

    .hint {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .output {
      min-height: 180px;
      max-height: 52vh;
      overflow: auto;
      border: 1px solid #28231f;
      border-radius: 8px;
      padding: 12px;
      background: #151310;
      color: #f5f0e8;
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .hidden {
      display: none;
    }

    @media (max-width: 520px) {
      .shell {
        padding-inline: 12px;
      }

      .row,
      .toolbar {
        grid-template-columns: 1fr;
      }

      h1 {
        font-size: 22px;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>Simple-Hyper</h1>
      <div id="status" class="status">未连接</div>
    </header>

    <section id="loginPanel" class="panel">
      <div class="field">
        <label for="token">访问口令</label>
        <input id="token" type="password" autocomplete="off" placeholder="Simple-Hyper token">
      </div>
      <div class="toolbar">
        <button id="saveToken" class="button primary">进入</button>
        <button id="clearToken" class="button ghost">清除</button>
      </div>
    </section>

    <section class="panel">
      <button id="query" class="button primary" style="width: 100%;">查询账户</button>
    </section>

    <section class="panel">
      <div class="row">
        <div class="field">
          <label for="coin">标的</label>
          <input id="coin" list="coinList" value="BTC" autocapitalize="characters">
          <datalist id="coinList"></datalist>
        </div>
        <div class="field">
          <label for="amount">金额 USDC</label>
          <input id="amount" type="number" inputmode="decimal" min="10" step="1" value="10">
        </div>
      </div>

      <div class="row">
        <div class="field">
          <label>方向</label>
          <div class="segmented">
            <button id="sideBuy" class="active buy" type="button">买入</button>
            <button id="sideSell" class="sell" type="button">卖出</button>
          </div>
        </div>
        <div class="field">
          <label>方式</label>
          <div class="toggle">
            <span>市价 IOC</span>
            <input id="market" type="checkbox" checked>
          </div>
        </div>
      </div>

      <div class="toolbar">
        <button id="dryRun" class="button ghost">预演</button>
        <button id="submitOrder" class="button buy">真实下单</button>
      </div>
      <p class="hint">真实下单会在服务器端再次校验确认词。</p>
    </section>

    <section class="panel">
      <div class="row">
        <div class="field">
          <label for="cancelCoin">撤单标的</label>
          <input id="cancelCoin" list="coinList" value="BTC" autocapitalize="characters">
        </div>
        <div class="field">
          <label for="oid">订单 ID</label>
          <input id="oid" inputmode="numeric" placeholder="留空撤该标的全部挂单">
        </div>
      </div>
      <div class="toolbar">
        <button id="dryCancel" class="button ghost">预演撤单</button>
        <button id="submitCancel" class="button danger">确认撤单</button>
      </div>
    </section>

    <section class="panel">
      <pre id="output" class="output">Ready.</pre>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = {
      token: localStorage.getItem("simpleHyperToken") || "",
      side: "buy",
      config: { default_amount: "10", coins: ["BTC", "ETH", "HYPE", "GOLD", "SAMSUNG", "QQQ"] },
    };

    function setStatus(text, ready = false) {
      $("status").textContent = text;
      $("status").classList.toggle("ready", ready);
    }

    function setOutput(text) {
      $("output").textContent = text || "";
      $("output").scrollTop = 0;
    }

    function setBusy(busy) {
      for (const id of ["query", "dryRun", "submitOrder", "dryCancel", "submitCancel"]) {
        $(id).disabled = busy;
      }
      setStatus(busy ? "执行中" : (state.token ? "已连接" : "未连接"), Boolean(state.token) && !busy);
    }

    function syncLogin() {
      $("token").value = state.token;
      $("loginPanel").classList.toggle("hidden", Boolean(state.token));
      setStatus(state.token ? "已连接" : "未连接", Boolean(state.token));
    }

    function syncSide() {
      $("sideBuy").classList.toggle("active", state.side === "buy");
      $("sideSell").classList.toggle("active", state.side === "sell");
      $("submitOrder").classList.toggle("buy", state.side === "buy");
      $("submitOrder").classList.toggle("sell", state.side === "sell");
    }

    function fillCoins(coins) {
      $("coinList").innerHTML = "";
      for (const coin of coins) {
        const option = document.createElement("option");
        option.value = coin;
        $("coinList").appendChild(option);
      }
    }

    async function api(path, payload = null) {
      if (!state.token) {
        throw new Error("missing token");
      }
      const response = await fetch(path, {
        method: payload ? "POST" : "GET",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${state.token}`,
        },
        body: payload ? JSON.stringify(payload) : undefined,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    async function run(path, payload, title) {
      try {
        setBusy(true);
        const data = await api(path, payload);
        const command = data.command ? `$ ${data.command}\n\n` : "";
        const elapsed = data.elapsed_ms === undefined ? "" : `\n\n[${data.elapsed_ms} ms]`;
        setOutput(`${title}\n\n${command}${data.output || ""}${elapsed}`);
      } catch (error) {
        setOutput(`${title}\n\nerror: ${error.message}`);
      } finally {
        setBusy(false);
      }
    }

    function orderPayload(dryRun) {
      return {
        coin: $("coin").value.trim(),
        side: state.side,
        amount: $("amount").value.trim(),
        market: $("market").checked,
        dry_run: dryRun,
        confirm: dryRun ? "" : "SUBMIT",
      };
    }

    function cancelPayload(dryRun) {
      return {
        coin: $("cancelCoin").value.trim(),
        oid: $("oid").value.trim(),
        dry_run: dryRun,
        confirm: dryRun ? "" : "CANCEL",
      };
    }

    async function loadConfig() {
      if (!state.token) return;
      try {
        const data = await api("/api/config");
        state.config = data;
        $("amount").value = data.default_amount || "10";
        fillCoins(data.coins || []);
      } catch (_error) {
        fillCoins(state.config.coins);
      }
    }

    $("saveToken").addEventListener("click", async () => {
      state.token = $("token").value.trim();
      localStorage.setItem("simpleHyperToken", state.token);
      syncLogin();
      await loadConfig();
    });

    $("clearToken").addEventListener("click", () => {
      state.token = "";
      localStorage.removeItem("simpleHyperToken");
      syncLogin();
    });

    $("sideBuy").addEventListener("click", () => {
      state.side = "buy";
      syncSide();
    });

    $("sideSell").addEventListener("click", () => {
      state.side = "sell";
      syncSide();
    });

    $("query").addEventListener("click", () => run("/api/query", {}, "账户查询"));
    $("dryRun").addEventListener("click", () => run("/api/order", orderPayload(true), "下单预演"));
    $("submitOrder").addEventListener("click", () => {
      if (confirm("确认真实提交这笔订单？")) {
        run("/api/order", orderPayload(false), "真实下单");
      }
    });
    $("dryCancel").addEventListener("click", () => run("/api/cancel", cancelPayload(true), "撤单预演"));
    $("submitCancel").addEventListener("click", () => {
      if (confirm("确认真实撤单？")) {
        run("/api/cancel", cancelPayload(false), "真实撤单");
      }
    });

    fillCoins(state.config.coins);
    syncSide();
    syncLogin();
    loadConfig();
  </script>
</body>
</html>
"""


MANIFEST = {
    "name": "Simple-Hyper",
    "short_name": "Simple-Hyper",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f7f4ef",
    "theme_color": "#f7f4ef",
}


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<rect width="128" height="128" rx="24" fill="#f7f4ef"/>
<path d="M24 80 50 32l18 64 36-48" fill="none" stroke="#245f73" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="50" cy="32" r="7" fill="#137547"/>
<circle cx="68" cy="96" r="7" fill="#b3261e"/>
</svg>"""


def json_bytes(payload: dict[str, Any], status: int = HTTPStatus.OK) -> tuple[int, bytes, str]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"


def normalize_coin(raw: Any) -> str:
    coin = str(raw or "").strip()
    if not COIN_RE.fullmatch(coin):
        raise ValueError("invalid coin")
    return coin


def normalize_amount(raw: Any) -> str:
    try:
        amount = Decimal(str(raw or "").strip())
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc
    if amount <= 0:
        raise ValueError("amount must be positive")
    if amount > MAX_AMOUNT_USD:
        raise ValueError(f"amount exceeds SIMPLE_HYPER_MAX_AMOUNT_USD={MAX_AMOUNT_USD}")
    return format(amount, "f")


def normalize_positive_decimal(raw: Any, name: str) -> str:
    try:
        value = Decimal(str(raw or "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return format(value, "f")


def run_hl_order(args: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    command = [sys.executable, str(PROJECT_DIR / "hl_order.py"), *args]
    completed = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    display_command = " ".join(["hl_order.py", *args])
    return {
        "ok": True,
        "command_ok": completed.returncode == 0,
        "command": display_command,
        "output": completed.stdout,
        "elapsed_ms": elapsed_ms,
        "returncode": completed.returncode,
    }


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise ValueError("invalid content length") from exc
    if length > 8192:
        raise ValueError("request body too large")
    body = handler.rfile.read(length) if length else b"{}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(payload, dict):
        raise ValueError("json body must be an object")
    return payload


class SimpleHyperHandler(BaseHTTPRequestHandler):
    server_version = "SimpleHyper/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def send_payload(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        response_status, body, content_type = json_bytes(payload, status)
        self.send_payload(response_status, body, content_type)

    def authorized(self) -> bool:
        if not TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix):
            return False
        return hmac.compare_digest(auth[len(prefix) :].strip(), TOKEN)

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_payload(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            self.send_payload(
                HTTPStatus.OK,
                json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"),
                "application/manifest+json; charset=utf-8",
            )
            return
        if path == "/icon.svg":
            self.send_payload(HTTPStatus.OK, ICON_SVG.encode("utf-8"), "image/svg+xml; charset=utf-8")
            return
        if path == "/api/health":
            self.send_json({"ok": True, "service": "simple-hyper"})
            return
        if path == "/api/config":
            if not self.require_auth():
                return
            self.send_json(
                {
                    "ok": True,
                    "default_amount": DEFAULT_AMOUNT,
                    "coins": DEFAULT_COINS,
                    "max_amount_usd": str(MAX_AMOUNT_USD),
                }
            )
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.require_auth():
            return
        try:
            payload = parse_json_body(self)
            if path == "/api/query":
                self.send_json(run_hl_order(["query"]))
                return
            if path == "/api/order":
                self.handle_order(payload)
                return
            if path == "/api/cancel":
                self.handle_cancel(payload)
                return
            self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except subprocess.TimeoutExpired:
            self.send_json({"ok": False, "error": "command timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_order(self, payload: dict[str, Any]) -> None:
        coin = normalize_coin(payload.get("coin"))
        amount = normalize_amount(payload.get("amount", DEFAULT_AMOUNT))
        side = str(payload.get("side", "")).strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        dry_run = bool(payload.get("dry_run", True))
        args = [coin, side, amount]
        if bool(payload.get("market", True)):
            args.append("--market")
        if payload.get("reduce_only"):
            args.append("--reduce-only")
        price = str(payload.get("price", "")).strip()
        if price:
            args.extend(["--price", normalize_positive_decimal(price, "price")])
        if dry_run:
            args.append("--dry-run")
        elif payload.get("confirm") != "SUBMIT":
            raise ValueError("real order requires confirm=SUBMIT")
        self.send_json(run_hl_order(args))

    def handle_cancel(self, payload: dict[str, Any]) -> None:
        coin = normalize_coin(payload.get("coin"))
        oid = str(payload.get("oid", "")).strip()
        if oid and not OID_RE.fullmatch(oid):
            raise ValueError("invalid oid")
        dry_run = bool(payload.get("dry_run", True))
        args = [coin, "--cancel"]
        if oid:
            args.append(oid)
        if dry_run:
            args.append("--dry-run")
        elif payload.get("confirm") != "CANCEL":
            raise ValueError("real cancel requires confirm=CANCEL")
        self.send_json(run_hl_order(args))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Simple-Hyper mobile web server.")
    parser.add_argument("--host", default=os.environ.get("SIMPLE_HYPER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIMPLE_HYPER_PORT", "8787")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not TOKEN:
        raise SystemExit("SIMPLE_HYPER_TOKEN is required")
    server = ThreadingHTTPServer((args.host, args.port), SimpleHyperHandler)
    scheme = "http"
    if TLS_CERT or TLS_KEY:
        if not TLS_CERT or not TLS_KEY:
            raise SystemExit("SIMPLE_HYPER_TLS_CERT and SIMPLE_HYPER_TLS_KEY must be set together")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(TLS_CERT, TLS_KEY)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"Simple-Hyper listening on {scheme}://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
