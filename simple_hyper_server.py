#!/usr/bin/env python3
"""Minimal mobile web command console for the local Hyperliquid order helper."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
ICON_FILES = {
    "/apple-touch-icon.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/apple-touch-icon-180.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/apple-touch-icon-precomposed.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/apple-touch-icon-120x120.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/apple-touch-icon-152x152.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/apple-touch-icon-167x167.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/apple-touch-icon-180x180.png": PROJECT_DIR / "simple-hyper-icon-180.png",
    "/icon.png": PROJECT_DIR / "simple-hyper-icon-192.png",
    "/icon-192.png": PROJECT_DIR / "simple-hyper-icon-192.png",
    "/icon-512.png": PROJECT_DIR / "simple-hyper-icon-512.png",
    "/favicon.ico": PROJECT_DIR / "simple-hyper-icon-192.png",
}
TLS_CERT = os.environ.get("SIMPLE_HYPER_TLS_CERT", "")
TLS_KEY = os.environ.get("SIMPLE_HYPER_TLS_KEY", "")
COMMAND_TIMEOUT = float(os.environ.get("SIMPLE_HYPER_COMMAND_TIMEOUT", "60"))
MAX_COMMAND_LENGTH = int(os.environ.get("SIMPLE_HYPER_MAX_COMMAND_LENGTH", "240"))
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SECRET_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#f6f2ea">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Simple-Hyper">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="application-name" content="Simple-Hyper">
  <title>Simple-Hyper</title>
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" href="/icon-192.png" sizes="192x192" type="image/png">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon-180.png">
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f2ea;
      --panel: #fffdf8;
      --ink: #151515;
      --muted: #675f55;
      --line: #d8cfc2;
      --accent: #245f73;
      --accent-2: #137547;
      --terminal: #151310;
      --terminal-ink: #f6efe5;
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
    input {
      font: inherit;
    }

    .shell {
      width: min(100%, 720px);
      margin: 0 auto;
      padding: 18px 12px 28px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }

    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.12;
      font-weight: 760;
    }

    .status {
      min-width: 72px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.58);
      text-align: center;
      font-size: 13px;
      white-space: nowrap;
    }

    .status.ready {
      color: var(--accent-2);
      border-color: rgba(19, 117, 71, 0.28);
      background: rgba(19, 117, 71, 0.08);
    }

    .panel {
      margin: 10px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    .hidden {
      display: none;
    }

    .field {
      display: grid;
      gap: 6px;
      margin-bottom: 10px;
    }

    label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 660;
    }

    input {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px 11px;
      background: #fff;
      color: var(--ink);
      outline: none;
    }

    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(36, 95, 115, 0.14);
    }

    .buttons {
      display: flex;
      gap: 10px;
      margin-top: 6px;
    }

    .verified-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: start;
    }

    .verify-copy {
      margin: 0 0 8px;
      color: var(--accent-2);
      font-size: 13px;
      font-weight: 720;
    }

    button {
      min-height: 46px;
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 10px 12px;
      font-weight: 760;
      color: #fff;
      background: var(--accent);
      white-space: nowrap;
    }

    button.small {
      min-height: 36px;
      padding: 7px 10px;
      font-size: 12px;
    }

    button.flex {
      flex: 1 1 auto;
    }

    button.narrow {
      flex: 0 0 86px;
    }

    button.secondary {
      color: var(--accent);
      border-color: var(--line);
      background: #fff;
    }

    button:disabled {
      opacity: 0.52;
    }

    button:active {
      transform: translateY(1px);
    }

    .output {
      min-height: 260px;
      max-height: 54vh;
      overflow: auto;
      -webkit-overflow-scrolling: touch;
      margin: 0;
      border: 1px solid #27231f;
      border-radius: 8px;
      padding: 12px;
      background: var(--terminal);
      color: var(--terminal-ink);
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre;
      overflow-wrap: normal;
      word-break: normal;
    }

    .footer {
      margin-top: 14px;
      text-align: center;
      font-size: 13px;
    }

    .footer a {
      color: var(--accent);
      font-weight: 720;
      text-decoration: none;
    }

    @media (max-width: 520px) {
      h1 {
        font-size: 22px;
      }

      .buttons {
        grid-template-columns: 1fr;
      }

      .verified-row {
        grid-template-columns: 1fr auto;
      }

      .output-panel {
        padding: 8px;
      }

      .output {
        min-height: 340px;
        max-height: 58vh;
        padding: 10px;
        font-size: 10px;
        line-height: 1.5;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>Simple-Hyper</h1>
      <div id="status" class="status">Not verified</div>
    </header>

    <section id="authPanel" class="panel">
      <div class="field">
        <label for="account">Wallet Address</label>
        <input id="account" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="0x...">
      </div>
      <div class="field">
        <label for="secret">Private Key or Agent Key</label>
        <input id="secret" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="0x...">
      </div>
      <div class="buttons">
        <button id="verify">Verify</button>
      </div>
    </section>

    <section id="verifiedPanel" class="panel hidden">
      <div class="verified-row">
        <div>
          <p class="verify-copy">Verification successful.</p>
          <button id="query" style="width: 100%;">Query Account</button>
        </div>
        <button id="reverify" class="secondary small">Re-verify</button>
      </div>
    </section>

    <section class="panel">
      <div class="field">
        <label for="command">Command</label>
        <input id="command" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="BTC buy 10 --dry-run">
      </div>
      <div class="buttons">
        <button id="clear" class="secondary small narrow">Clear</button>
        <button id="submit" class="flex">Run</button>
      </div>
    </section>

    <section class="panel output-panel">
      <pre id="output" class="output">Ready.</pre>
    </section>

    <footer class="footer">
      <a href="/readme">README</a>
    </footer>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = {
      account_address: "",
      secret_key: "",
      verified: false,
    };

    function setStatus(text, ready = false) {
      $("status").textContent = text;
      $("status").classList.toggle("ready", ready);
    }

    function setOutput(text) {
      $("output").textContent = text || "";
      $("output").scrollTop = 0;
    }

    function credentials() {
      return {
        account_address: state.verified ? state.account_address : $("account").value.trim(),
        secret_key: state.verified ? state.secret_key : $("secret").value.trim(),
      };
    }

    function setBusy(busy) {
      $("submit").disabled = busy;
      $("verify").disabled = busy;
      $("query").disabled = busy;
      $("reverify").disabled = busy;
      if (busy) {
        setStatus("Running", false);
      } else {
        setStatus(state.verified ? "Verified" : "Not verified", state.verified);
      }
    }

    function syncAuth() {
      $("authPanel").classList.toggle("hidden", state.verified);
      $("verifiedPanel").classList.toggle("hidden", !state.verified);
      setStatus(state.verified ? "Verified" : "Not verified", state.verified);
    }

    async function apiRun(command) {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...credentials(), command }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    function renderRun(data) {
      const shownCommand = data.command ? `$ ${data.command}\n\n` : "";
      const elapsed = data.elapsed_ms === undefined ? "" : `\n\n[${data.elapsed_ms} ms]`;
      setOutput(`${shownCommand}${data.output || ""}${elapsed}`);
    }

    function isReadOnlyCommand(command) {
      if (command === "query") return true;
      if (!command.includes(" ") && command.length > 0) return true;
      return command.includes("--dry-run");
    }

    async function run(command) {
      if (!state.verified) {
        setOutput("Verify your wallet first.");
        return;
      }
      try {
        setBusy(true);
        renderRun(await apiRun(command));
      } catch (error) {
        setOutput(`error: ${error.message}`);
      } finally {
        setBusy(false);
      }
    }

    async function verify() {
      state.account_address = $("account").value.trim();
      state.secret_key = $("secret").value.trim();
      try {
        setBusy(true);
        const data = await apiRun("query");
        if (!data.command_ok) {
          throw new Error(data.output || "Verification failed.");
        }
        state.verified = true;
        syncAuth();
        renderRun(data);
      } catch (error) {
        state.verified = false;
        syncAuth();
        setOutput(`Verification failed.\n\n${error.message}`);
      } finally {
        setBusy(false);
      }
    }

    function reverify() {
      state.verified = false;
      state.account_address = "";
      state.secret_key = "";
      $("account").value = "";
      $("secret").value = "";
      syncAuth();
      setOutput("Ready.");
    }

    function clearCommand() {
      const input = $("command");
      input.value = "";
      input.focus();
      input.setSelectionRange(0, 0);
    }

    $("verify").addEventListener("click", verify);
    $("reverify").addEventListener("click", reverify);
    $("clear").addEventListener("click", clearCommand);
    $("submit").addEventListener("click", () => {
      const command = $("command").value.trim();
      if (command && !isReadOnlyCommand(command)) {
        if (!confirm("This command may submit a real action. Continue?")) return;
      }
      run(command);
    });
    $("query").addEventListener("click", () => run("query"));
    $("command").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        $("submit").click();
      }
    });

    syncAuth();
  </script>
</body>
</html>
"""


README_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f6f2ea">
  <title>Simple-Hyper README</title>
  <style>
    body {
      margin: 0;
      background: #f6f2ea;
      color: #151515;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    main {
      width: min(100%, 720px);
      margin: 0 auto;
      padding: 22px 14px 34px;
    }
    h1 {
      margin: 0 0 16px;
      font-size: 24px;
    }
    section {
      margin: 12px 0;
      padding: 14px;
      border: 1px solid #d8cfc2;
      border-radius: 8px;
      background: #fffdf8;
    }
    h2 {
      margin: 0 0 10px;
      font-size: 15px;
    }
    p, li {
      color: #675f55;
      line-height: 1.5;
      font-size: 14px;
    }
    code {
      color: #151515;
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 13px;
    }
    ul {
      padding-left: 20px;
    }
    a {
      color: #245f73;
      font-weight: 720;
      text-decoration: none;
    }
  </style>
</head>
<body>
  <main>
    <h1>Simple-Hyper README</h1>
    <section>
      <h2>Command Shape</h2>
      <ul>
        <li><code>coin</code>, for example <code>BTC</code></li>
        <li><code>buy/sell</code>, or <code>query</code></li>
        <li><code>amount</code>, default <code>10</code></li>
        <li><code>entry/exec</code> options: <code>--market</code>, <code>--price</code>, <code>--stop</code>, <code>--stop-limit</code>, <code>--take</code>, <code>--take-limit</code>, <code>--level</code>, <code>--tif</code>, <code>--slippage</code></li>
        <li><code>tp/sl</code>: <code>--tp</code>, <code>--sl</code></li>
        <li><code>--reduce-only</code></li>
      </ul>
      <p><code>--stop</code> / <code>--take</code> are entry triggers. <code>--stop</code> is breakout style and <code>--take</code> is if-touched style. You can append <code>+50</code>, <code>-50</code>, <code>+0.2%</code>, or <code>-0.2%</code> to set the post-trigger limit price; without a suffix the order is a market trigger. <code>--stop-limit</code> / <code>--take-limit</code> still work as explicit equivalents. Percent is literal: <code>70000+2%</code> means <code>71400</code>, while <code>70140</code> is <code>70000+0.2%</code>. These trigger-limit styles are not <code>ALO</code>; only plain limit orders use <code>--tif</code>. <code>--tp</code> / <code>--sl</code> also accept absolute prices, absolute prices plus offsets, or relative percentages from the entry / position price such as <code>2%+0.1%</code> and <code>-2%-0.1%</code>. Without <code>--reduce-only</code> they form a bracket order, and with it they protect an existing position. <code>--level</code> is the main name for the same-side book depth; <code>--book-level</code> still works as an alias.</p>
    </section>
    <section>
      <h2>Flow</h2>
      <p>Enter your wallet address and private key or agent key, then tap Verify. The server does not save these credentials.</p>
    </section>
    <section>
      <h2>Examples</h2>
      <ul>
        <li><code>BTC</code></li>
        <li><code>query</code></li>
        <li><code>BTC buy 10 --dry-run</code></li>
        <li><code>BTC buy 10 --market --dry-run</code></li>
        <li><code>BTC buy 10 --price 75000 --dry-run</code></li>
        <li><code>BTC sell 10 --market --dry-run</code></li>
        <li><code>BTC sell 25 --stop 70000 --dry-run</code></li>
        <li><code>BTC buy 25 --stop 80000 --dry-run</code></li>
        <li><code>BTC sell 25 --stop 70000+50 --dry-run</code></li>
        <li><code>BTC buy 25 --stop 80000+0.2% --dry-run</code></li>
        <li><code>BTC buy 25 --take 70000 --dry-run</code></li>
        <li><code>BTC sell 25 --take 80000 --dry-run</code></li>
        <li><code>BTC buy 25 --take 70000+50 --dry-run</code></li>
        <li><code>BTC sell 25 --take 80000+100 --dry-run</code></li>
        <li><code>BTC sell 25 --sl 65000 --reduce-only --dry-run</code></li>
        <li><code>BTC sell --tp 2%+0.1% --sl -2%-0.1% --reduce-only --dry-run</code></li>
        <li><code>BTC buy --tp 2%+0.1% --sl -2%-0.1% --dry-run</code></li>
        <li><code>BTC sell --tp -2%-0.1% --sl 2%+0.1% --dry-run</code></li>
        <li><code>BTC buy 100 --price 68000 --tp 72000 --sl 65000 --dry-run</code></li>
        <li><code>BTC buy 100 --scale 5 --from 67000 --to 63000 --dry-run</code></li>
        <li><code>BTC --cancel --dry-run</code></li>
        <li><code>BTC --cancel 441260592983 --dry-run</code></li>
      </ul>
    </section>
    <section>
      <h2>Notes</h2>
      <ul>
        <li>A coin-only command such as <code>BTC</code> returns the 24h trend, high, low, turnover, and current position if any.</li>
        <li>Use <code>--dry-run</code> to preview without submitting.</li>
        <li>Limit orders default to <code>ALO</code> unless you pass <code>--tif</code>.</li>
        <li>Commands without <code>--dry-run</code> can place or cancel real orders, except read-only commands like <code>query</code> or <code>BTC</code>.</li>
        <li>Market orders use <code>--market</code> and Hyperliquid IOC behavior.</li>
        <li><code>--stop-entry</code> or <code>--stop</code> creates a breakout-style entry trigger. Without a suffix it becomes a market trigger; with a suffix such as <code>+50</code> or <code>+0.2%</code> it becomes a trigger-limit order.</li>
        <li><code>--take-entry</code> or <code>--take</code> creates an if-touched entry trigger. Without a suffix it becomes a market trigger; with a suffix such as <code>+50</code> or <code>+0.2%</code> it becomes a trigger-limit order.</li>
        <li>Percent offsets are literal: <code>70000+2%</code> means <code>71400</code>, while <code>70140</code> is <code>70000+0.2%</code>.</li>
        <li>Percentage-derived trigger and limit prices are snapped to the exchange's accepted price precision before submission.</li>
        <li>Trigger-limit orders are not <code>ALO</code>; only plain limit orders use <code>--tif</code>.</li>
        <li><code>--tp</code> / <code>--sl</code> can use absolute prices, absolute prices plus offsets, or relative percentages from the entry / position price such as <code>2%+0.1%</code> and <code>-2%-0.1%</code>.</li>
        <li><code>--tp</code> / <code>--sl</code> without <code>--reduce-only</code> create a bracket order; with <code>--reduce-only</code> they create protective position TP/SL orders.</li>
        <li><code>--level</code> is the main name for the same-side book depth; <code>--book-level</code> still works as an alias.</li>
        <li><code>--scale</code> splits a total USD amount into multiple limit orders.</li>
        <li>The command box is parsed as <code>hl_order.py</code> arguments, not as a shell command.</li>
      </ul>
    </section>
    <p><a href="/">Back to Simple-Hyper</a></p>
  </main>
</body>
</html>
"""


MANIFEST = {
    "id": "/",
    "name": "Simple-Hyper",
    "short_name": "Simple-Hyper",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#f6f2ea",
    "theme_color": "#f6f2ea",
    "icons": [
        {
            "src": "/apple-touch-icon-180.png",
            "sizes": "180x180",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        }
    ],
}


def json_bytes(payload: dict[str, Any], status: int = HTTPStatus.OK) -> tuple[int, bytes, str]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"


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


def normalize_credentials(payload: dict[str, Any]) -> tuple[str, str]:
    account_address = str(payload.get("account_address", "")).strip()
    secret_key = str(payload.get("secret_key", "")).strip()
    if not ADDRESS_RE.fullmatch(account_address):
        raise ValueError("invalid wallet address")
    if not SECRET_RE.fullmatch(secret_key):
        raise ValueError("invalid key")
    return account_address, secret_key


def parse_command(raw: Any) -> list[str]:
    command = str(raw or "").strip()
    if not command:
        raise ValueError("command is required")
    if len(command) > MAX_COMMAND_LENGTH:
        raise ValueError("command is too long")
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"invalid command: {exc}") from exc
    if not args:
        raise ValueError("command is required")
    if args[0] in {"./hl_order.py", "hl_order.py", "order"}:
        args = args[1:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        raise ValueError("command is required")
    return args


def clean_web_output(output: str) -> str:
    lines = [line for line in output.splitlines() if not line.startswith("log: ")]
    return "\n".join(lines).rstrip()


def run_hl_order(args: list[str], account_address: str, secret_key: str) -> dict[str, Any]:
    started = time.monotonic()
    command = [sys.executable, str(PROJECT_DIR / "hl_order.py"), *args]
    child_env = os.environ.copy()
    child_env["account_address"] = account_address
    child_env["secret_key"] = secret_key
    completed = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "command_ok": completed.returncode == 0,
        "command": f"hl_order.py {shlex.join(args)}",
        "output": clean_web_output(completed.stdout),
        "elapsed_ms": elapsed_ms,
        "returncode": completed.returncode,
    }


class SimpleHyperHandler(BaseHTTPRequestHandler):
    server_version = "SimpleHyper/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def send_headers(
        self,
        status: int,
        content_length: int,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def send_payload(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_headers(status, len(body), content_type, cache_control)
        self.wfile.write(body)

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        response_status, body, content_type = json_bytes(payload, status)
        self.send_payload(response_status, body, content_type)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_payload(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/readme":
            self.send_payload(HTTPStatus.OK, README_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            self.send_payload(
                HTTPStatus.OK,
                json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"),
                "application/manifest+json; charset=utf-8",
                "public, max-age=3600",
            )
            return
        if path in ICON_FILES:
            self.send_payload(HTTPStatus.OK, ICON_FILES[path].read_bytes(), "image/png", "public, max-age=86400")
            return
        if path == "/api/health":
            self.send_json({"ok": True, "service": "simple-hyper"})
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_headers(HTTPStatus.OK, len(INDEX_HTML.encode("utf-8")), "text/html; charset=utf-8")
            return
        if path == "/readme":
            self.send_headers(HTTPStatus.OK, len(README_HTML.encode("utf-8")), "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            body = json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8")
            self.send_headers(
                HTTPStatus.OK,
                len(body),
                "application/manifest+json; charset=utf-8",
                "public, max-age=3600",
            )
            return
        if path in ICON_FILES:
            self.send_headers(
                HTTPStatus.OK,
                ICON_FILES[path].stat().st_size,
                "image/png",
                "public, max-age=86400",
            )
            return
        self.send_headers(HTTPStatus.NOT_FOUND, 0, "application/json; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path != "/api/run":
                self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            payload = parse_json_body(self)
            account_address, secret_key = normalize_credentials(payload)
            args = parse_command(payload.get("command"))
            self.send_json(run_hl_order(args, account_address, secret_key))
        except subprocess.TimeoutExpired:
            self.send_json({"ok": False, "error": "command timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Simple-Hyper mobile web server.")
    parser.add_argument("--host", default=os.environ.get("SIMPLE_HYPER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIMPLE_HYPER_PORT", "8787")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
