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
from threading import Lock
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
COMMAND_TIMEOUT = float(os.environ.get("SIMPLE_HYPER_COMMAND_TIMEOUT", "300"))
MAX_COMMAND_LENGTH = int(os.environ.get("SIMPLE_HYPER_MAX_COMMAND_LENGTH", "240"))
COMMAND_HISTORY_LIMIT = int(os.environ.get("SIMPLE_HYPER_COMMAND_HISTORY_LIMIT", "30"))
COMMAND_HISTORY_PATH = Path(os.environ.get("SIMPLE_HYPER_COMMAND_HISTORY", str(PROJECT_DIR / "command_history.json")))
COMMAND_HISTORY_LOCK = Lock()
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
    input,
    select {
      font: inherit;
    }

    .shell {
      width: min(100%, 720px);
      margin: 0 auto;
      padding: 18px 12px 28px;
    }

    .content-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      align-items: start;
    }

    .header-block {
      grid-column: 1 / -1;
    }

    .auth-block,
    .verified-block,
    .command-block,
    .footer-block,
    .output-block {
      min-width: 0;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;
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

    .button-link {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px 10px;
      color: var(--accent);
      background: #fff;
      font-size: 12px;
      font-weight: 760;
      text-decoration: none;
      white-space: nowrap;
    }

    .panel {
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

    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(36, 95, 115, 0.14);
    }

    .credential-proxy {
      position: absolute;
      left: -9999px;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }

    .buttons {
      display: flex;
      gap: 10px;
      margin-top: 6px;
    }

    .history {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
    }

    .history.hidden {
      display: none;
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

    button.history-item {
      min-height: 32px;
      padding: 6px 8px;
      font-size: 12px;
      font-weight: 700;
      color: var(--accent);
      border-color: var(--line);
      background: #fff;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
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

    @media (orientation: landscape) and (min-width: 720px) {
      .shell {
        width: min(100%, 1320px);
        padding-inline: 18px;
      }

      .content-grid {
        grid-template-columns: minmax(360px, 0.95fr) minmax(520px, 1.25fr);
        grid-template-areas:
          "header header"
          "auth output"
          "verified output"
          "command output"
          "footer output";
        gap: 12px;
      }

      .header-block {
        grid-area: header;
      }

      .auth-block {
        grid-area: auth;
      }

      .verified-block {
        grid-area: verified;
      }

      .command-block {
        grid-area: command;
      }

      .output-block {
        grid-area: output;
        position: sticky;
        top: 12px;
        align-self: start;
      }

      .footer-block {
        grid-area: footer;
      }

      .output {
        min-height: calc(100vh - 94px);
        max-height: calc(100vh - 94px);
      }
    }
  </style>
</head>
<body>
  <main class="shell content-grid">
    <header class="header-block">
      <h1>Simple-Hyper</h1>
      <div class="header-actions">
        <a class="button-link" href="/grid">Grid</a>
        <div id="status" class="status">Not verified</div>
      </div>
    </header>

    <form id="authPanel" class="panel auth-block" method="post" action="/api/run" autocomplete="off">
      <p class="verify-copy">Using the wallet and private key saved on this server.</p>
      <div class="buttons">
        <button id="verify" type="submit">Verify</button>
      </div>
    </form>

    <section id="verifiedPanel" class="panel verified-block hidden">
      <div class="verified-row">
        <div>
          <p class="verify-copy">Verification successful.</p>
          <button id="query" style="width: 100%;">Query Account</button>
        </div>
        <button id="reverify" class="secondary small">Re-verify</button>
      </div>
    </section>

    <section class="panel command-block">
      <div class="field">
        <label for="command">Command</label>
        <input id="command" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="BTC buy 10 --dry-run">
      </div>
      <div class="buttons">
        <button id="clear" class="secondary small narrow">Clear</button>
        <button id="submit" class="flex">Run</button>
      </div>
      <div id="history" class="history hidden"></div>
    </section>

    <section class="panel output-panel output-block">
      <pre id="output" class="output">Ready.</pre>
    </section>

    <footer class="footer footer-block">
      <a href="/grid">Grid Detail</a> · <a href="/readme">README</a>
    </footer>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = {
      verified: false,
      command_history: [],
      command_history_index: -1,
      command_history_draft: "",
    };

    const COMMAND_HISTORY_LIMIT = 30;

    function setStatus(text, ready = false) {
      $("status").textContent = text;
      $("status").classList.toggle("ready", ready);
    }

    function setOutput(text) {
      $("output").textContent = text || "";
      $("output").scrollTop = 0;
    }

    function normalizeHistoryCommand(command) {
      return String(command || "").trim();
    }

    function applyHistory(items) {
      if (!Array.isArray(items)) {
        state.command_history = [];
        renderHistory();
        return;
      }
      state.command_history = items.map((item) => normalizeHistoryCommand(item)).filter(Boolean).slice(0, COMMAND_HISTORY_LIMIT);
      renderHistory();
    }

    async function loadHistory() {
      try {
        const response = await fetch("/api/history", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        applyHistory(data.history);
      } catch {
        applyHistory([]);
      }
    }

    async function saveHistory(command) {
      try {
        const response = await fetch("/api/history", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ command }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        applyHistory(data.history);
      } catch {
        // Keep the optimistic in-page history if the history endpoint is briefly unavailable.
      }
    }

    function resetHistory() {
      state.command_history_index = -1;
      state.command_history_draft = "";
      state.command_history = [];
      renderHistory();
    }

    function renderHistory() {
      const history = $("history");
      if (!state.command_history.length) {
        history.innerHTML = "";
        history.classList.add("hidden");
        return;
      }
      history.classList.remove("hidden");
      history.replaceChildren();
      const sortedHistory = [...state.command_history]
        .slice(0, COMMAND_HISTORY_LIMIT)
        .sort((left, right) => left.localeCompare(right, undefined, { sensitivity: "base" }));
      for (const command of sortedHistory) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "history-item";
        button.dataset.command = command;
        button.title = command;
        button.textContent = command;
        history.appendChild(button);
      }
    }

    function pushHistory(command) {
      const normalized = normalizeHistoryCommand(command);
      if (!normalized) return;
      const existing = state.command_history.indexOf(normalized);
      if (existing === 0) {
        state.command_history_index = -1;
        state.command_history_draft = "";
        saveHistory(normalized);
        renderHistory();
        return;
      }
      if (existing > 0) {
        state.command_history.splice(existing, 1);
      }
      state.command_history.unshift(normalized);
      if (state.command_history.length > COMMAND_HISTORY_LIMIT) {
        state.command_history.length = COMMAND_HISTORY_LIMIT;
      }
      state.command_history_index = -1;
      state.command_history_draft = "";
      saveHistory(normalized);
      renderHistory();
    }

    function recallHistory(direction) {
      if (!state.command_history.length) return;
      const input = $("command");
      if (state.command_history_index === -1) {
        state.command_history_draft = input.value;
      }
      if (direction < 0) {
        state.command_history_index = Math.min(state.command_history_index + 1, state.command_history.length - 1);
      } else {
        state.command_history_index = Math.max(state.command_history_index - 1, -1);
      }
      if (state.command_history_index === -1) {
        input.value = state.command_history_draft;
        return;
      }
      input.value = state.command_history[state.command_history_index];
      input.setSelectionRange(input.value.length, input.value.length);
    }

    function credentials() {
      return {};
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

    async function apiVerify() {
      const response = await fetch("/api/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
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
      const normalized = normalizeHistoryCommand(command);
      if (normalized) {
        pushHistory(normalized);
      }
      try {
        setBusy(true);
        renderRun(await apiRun(normalized));
      } catch (error) {
        setOutput(`error: ${error.message}`);
      } finally {
        setBusy(false);
      }
    }

    async function executeCommand(command) {
      const normalized = normalizeHistoryCommand(command);
      if (!normalized) return;
      if (!isReadOnlyCommand(normalized)) {
        if (!confirm("This command may submit a real action. Continue?")) return;
      }
      await run(normalized);
    }

    async function verify() {
      try {
        setBusy(true);
        await apiVerify();
        state.verified = true;
        syncAuth();
        await loadHistory();
        setOutput("Verified. Ready.");
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
      syncAuth();
      resetHistory();
      setOutput("Ready.");
    }

    function clearCommand() {
      const input = $("command");
      input.value = "";
      state.command_history_index = -1;
      state.command_history_draft = "";
      input.focus();
      input.setSelectionRange(0, 0);
    }

    $("authPanel").addEventListener("submit", (event) => {
      event.preventDefault();
      verify();
    });
    $("reverify").addEventListener("click", reverify);
    $("clear").addEventListener("click", clearCommand);
    $("history").addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLButtonElement)) return;
      const command = normalizeHistoryCommand(target.dataset.command);
      if (!command) return;
      const input = $("command");
      input.value = command;
      state.command_history_index = -1;
      state.command_history_draft = "";
      executeCommand(command);
    });
    $("submit").addEventListener("click", () => executeCommand($("command").value));
    $("query").addEventListener("click", () => run("query"));
    $("command").addEventListener("keydown", (event) => {
      if (event.key === "ArrowUp") {
        event.preventDefault();
        recallHistory(-1);
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        recallHistory(1);
        return;
      }
      if (event.key === "Enter") {
        $("submit").click();
      }
    });

    syncAuth();
    renderHistory();
  </script>
</body>
</html>
"""


GRID_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#eef1ed">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Grid Detail">
  <title>Simple-Hyper Grid Detail</title>
  <link rel="icon" href="/icon-192.png" sizes="192x192" type="image/png">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <style>
    :root {
      color-scheme: light;
      --bg: #eef1ed;
      --panel: #fffdf9;
      --ink: #141716;
      --muted: #5e6761;
      --line: #cbd4cc;
      --accent: #235c67;
      --buy: #126b47;
      --sell: #9f3333;
      --warn: #9a6a18;
      --terminal: #141716;
      --terminal-ink: #f4f0e8;
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
      width: min(100%, 1240px);
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
      font-size: 23px;
      line-height: 1.12;
      font-weight: 780;
    }

    a {
      color: var(--accent);
      font-weight: 740;
      text-decoration: none;
    }

    .status {
      min-width: 78px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.72);
      text-align: center;
      font-size: 13px;
      white-space: nowrap;
    }

    .status.ready {
      color: var(--buy);
      border-color: rgba(18, 107, 71, 0.28);
      background: rgba(18, 107, 71, 0.08);
    }

    .grid-layout {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      align-items: start;
    }

    .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      min-width: 0;
    }

    .toolbar {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }

    .field {
      display: grid;
      gap: 6px;
    }

    label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 680;
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

    input:focus,
    select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(35, 92, 103, 0.13);
    }

    .credential-proxy {
      position: absolute;
      left: -9999px;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }

    .actions {
      display: flex;
      gap: 8px;
      align-items: end;
    }

    button {
      min-height: 44px;
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 10px 12px;
      font-weight: 760;
      color: #fff;
      background: var(--accent);
      white-space: nowrap;
    }

    button.secondary {
      color: var(--accent);
      border-color: var(--line);
      background: #fff;
    }

    button.full {
      width: 100%;
    }

    button:disabled {
      opacity: 0.52;
    }

    button:active {
      transform: translateY(1px);
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
      gap: 8px;
    }

    .active-list {
      display: grid;
      gap: 8px;
    }

    .grid-card {
      width: 100%;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      color: var(--ink);
      background: #fff;
      text-align: left;
      white-space: normal;
    }

    .grid-card.selected {
      border-color: rgba(35, 92, 103, 0.54);
      box-shadow: 0 0 0 3px rgba(35, 92, 103, 0.11);
    }

    .grid-card-main {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: baseline;
      font-weight: 780;
    }

    .grid-card-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 5px 10px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.25;
    }

    .modify-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }

    .modify-grid .wide {
      grid-column: 1 / -1;
    }

    .command-preview {
      margin: 0;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 9px;
      background: #fff;
      color: var(--muted);
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 9px;
      min-height: 72px;
    }

    .metric .label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 720;
      text-transform: uppercase;
    }

    .metric .value {
      margin-top: 6px;
      overflow-wrap: anywhere;
      font-size: 14px;
      font-weight: 760;
      line-height: 1.25;
    }

    .section-title {
      margin: 0 0 10px;
      font-size: 14px;
      font-weight: 780;
    }

    .table-wrap {
      overflow: auto;
      -webkit-overflow-scrolling: touch;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 860px;
      font-size: 12px;
    }

    th,
    td {
      border-bottom: 1px solid var(--line);
      padding: 8px 7px;
      text-align: left;
      white-space: nowrap;
    }

    th {
      position: sticky;
      top: 0;
      z-index: 1;
      color: var(--muted);
      background: var(--panel);
      font-size: 11px;
      font-weight: 780;
    }

    td.price,
    td.value,
    td.size {
      font-variant-numeric: tabular-nums;
    }

    tr.mid td {
      color: var(--warn);
      background: rgba(154, 106, 24, 0.09);
      font-weight: 760;
    }

    .side-buy {
      color: var(--buy);
      font-weight: 760;
    }

    .side-sell {
      color: var(--sell);
      font-weight: 760;
    }

    .raw {
      min-height: 240px;
      max-height: 62vh;
      overflow: auto;
      -webkit-overflow-scrolling: touch;
      margin: 0;
      border: 1px solid #242723;
      border-radius: 8px;
      padding: 12px;
      background: var(--terminal);
      color: var(--terminal-ink);
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 11px;
      line-height: 1.52;
      white-space: pre;
    }

    .empty {
      color: var(--muted);
      font-size: 13px;
    }

    @media (min-width: 900px) {
      .grid-layout {
        grid-template-columns: minmax(320px, 0.8fr) minmax(560px, 1.2fr);
        grid-template-areas:
          "controls active"
          "modify summary"
          "raw orders";
      }

      .controls-panel {
        grid-area: controls;
      }

      .active-panel {
        grid-area: active;
      }

      .modify-panel {
        grid-area: modify;
      }

      .summary-panel {
        grid-area: summary;
      }

      .orders-panel {
        grid-area: orders;
      }

      .raw-panel {
        grid-area: raw;
        position: sticky;
        top: 12px;
      }
    }

    @media (max-width: 560px) {
      .shell {
        padding-inline: 10px;
      }

      h1 {
        font-size: 21px;
      }

      .actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
      }

      .raw {
        font-size: 10px;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>Grid Detail</h1>
      <div id="status" class="status">Ready</div>
    </header>

    <div class="grid-layout">
      <section class="panel controls-panel">
        <form id="gridForm" class="toolbar" autocomplete="off">
          <div class="field">
            <label for="coin">Coin</label>
            <input id="coin" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="BTC" value="BTC">
          </div>
          <div class="actions">
            <button id="refresh" type="submit">Load Active</button>
            <button id="detailRefresh" type="button" class="secondary">Detail</button>
            <button id="clear" type="button" class="secondary">Clear</button>
          </div>
        </form>
      </section>

      <section class="panel active-panel">
        <h2 class="section-title">Active Grids</h2>
        <div id="activeGrids" class="active-list">
          <div class="empty">Load active grids to pick a coin.</div>
        </div>
      </section>

      <section class="panel modify-panel">
        <h2 class="section-title">Quick Modify</h2>
        <form id="modifyForm" class="modify-grid" autocomplete="off">
          <div class="field">
            <label for="limitMode">Limit</label>
            <select id="limitMode">
              <option value="abs">abs</option>
              <option value="long">long</option>
              <option value="short">short</option>
            </select>
          </div>
          <div class="field">
            <label for="maxPosition">Max</label>
            <input id="maxPosition" inputmode="decimal" autocomplete="off" placeholder="300">
          </div>
          <div class="field">
            <label for="minPosition">Min</label>
            <input id="minPosition" inputmode="decimal" autocomplete="off" placeholder="0">
          </div>
          <div class="field">
            <label for="gap">Gap</label>
            <input id="gap" autocomplete="off" placeholder="0.5%">
          </div>
          <div class="field">
            <label for="minOrder">Min Order</label>
            <input id="minOrder" inputmode="decimal" autocomplete="off" placeholder="20">
          </div>
          <div class="field">
            <label for="strategyMode">Strategy</label>
            <select id="strategyMode">
              <option value="keep">keep</option>
              <option value="avg">avg</option>
              <option value="trend">trend</option>
            </select>
          </div>
          <div class="field wide">
            <label for="strategyValue">Strategy Value</label>
            <input id="strategyValue" autocomplete="off" placeholder="200 or 10%">
          </div>
          <div class="field wide">
            <label>Command</label>
            <pre id="modifyCommand" class="command-preview">Pick an active grid.</pre>
          </div>
          <button id="applyModify" type="submit" class="full wide" disabled>Apply Modify</button>
        </form>
      </section>

      <section class="panel summary-panel">
        <h2 class="section-title">Summary</h2>
        <div id="summary" class="summary">
          <div class="empty">Run a grid query to show current config, risk, spacing, position, and live oid counts.</div>
        </div>
      </section>

      <section class="panel orders-panel">
        <h2 class="section-title">Grid Orders</h2>
        <div id="orders" class="table-wrap">
          <div class="empty">No grid orders loaded.</div>
        </div>
      </section>

      <section class="panel raw-panel">
        <h2 class="section-title">Raw Output</h2>
        <pre id="raw" class="raw">Ready.</pre>
      </section>
    </div>

    <p class="empty" style="text-align:center; margin: 14px 0 0;"><a href="/">Main console</a> · <a href="/readme">README</a></p>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = {
      active_grids: [],
      selected_grid: null,
    };

    function setStatus(text, ready = false) {
      $("status").textContent = text;
      $("status").classList.toggle("ready", ready);
    }

    function credentials() {
      return {};
    }

    function setBusy(busy) {
      $("refresh").disabled = busy;
      $("detailRefresh").disabled = busy;
      $("clear").disabled = busy;
      $("applyModify").disabled = busy || !state.selected_grid;
      setStatus(busy ? "Loading" : "Ready", !busy);
    }

    function normalizeCoin(value) {
      return String(value || "").trim().toUpperCase();
    }

    async function apiGrid() {
      const response = await fetch("/api/grid", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...credentials(), coin: normalizeCoin($("coin").value) }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    async function apiGrids() {
      const response = await fetch("/api/grids", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(credentials()),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
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

    function cleanCell(value) {
      return String(value || "").trim();
    }

    function parseGridSummary(output) {
      const rows = [];
      const lines = String(output || "").split(/\r?\n/);
      let inGrid = false;
      for (const line of lines) {
        if (/^\+- Grid -+/.test(line)) {
          inGrid = true;
          rows.length = 0;
          continue;
        }
        if (inGrid && /^\+-/.test(line)) break;
        if (!inGrid || !line.startsWith("|")) continue;
        const text = line.replace(/^\|\s*/, "").replace(/\s*\|$/, "");
        const index = text.indexOf(":");
        if (index <= 0) continue;
        rows.push([cleanCell(text.slice(0, index)), cleanCell(text.slice(index + 1))]);
      }
      return rows;
    }

    function parseGridOrders(output) {
      const lines = String(output || "").split(/\r?\n/);
      const titleIndex = lines.findIndex((line) => /^\+- Grid Orders/.test(line));
      if (titleIndex < 0) return { columns: [], rows: [] };
      const tableLines = [];
      for (let i = titleIndex + 1; i < lines.length; i += 1) {
        const line = lines[i];
        if (/^\+- /.test(line) && tableLines.length) break;
        if (line.startsWith("|") || line.startsWith("+")) tableLines.push(line);
      }
      const pipeLines = tableLines.filter((line) => line.startsWith("|"));
      if (!pipeLines.length) return { columns: [], rows: [] };
      const columns = pipeLines[0].split("|").slice(1, -1).map(cleanCell);
      const rows = pipeLines.slice(1).map((line) => {
        const cells = line.split("|").slice(1, -1).map(cleanCell);
        return Object.fromEntries(columns.map((column, index) => [column, cells[index] || ""]));
      });
      return { columns, rows };
    }

    function metric(key, value) {
      const node = document.createElement("div");
      node.className = "metric";
      const label = document.createElement("div");
      label.className = "label";
      label.textContent = key;
      const body = document.createElement("div");
      body.className = "value";
      body.textContent = value || "-";
      node.append(label, body);
      return node;
    }

    function renderSummary(rows) {
      const summary = $("summary");
      summary.replaceChildren();
      if (!rows.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No grid summary found in command output.";
        summary.appendChild(empty);
        return;
      }
      const preferred = [
        "coin", "status", "limit", "position", "min", "avg", "avg_position", "avg_multiplier",
        "avg_side", "base_gap", "topup_gap", "base_size", "topup_size", "trend", "margin_gap",
        "roe", "roe_limit", "roe_allowed", "panic_ratio", "panic_threshold", "panic_reduced",
        "panic_last", "target_side", "active_buy", "active_sell", "live_oids", "updated", "note",
      ];
      const byKey = new Map(rows);
      for (const key of preferred) {
        if (byKey.has(key)) summary.appendChild(metric(key, byKey.get(key)));
      }
      for (const [key, value] of rows) {
        if (!preferred.includes(key)) summary.appendChild(metric(key, value));
      }
    }

    function renderOrders(parsed) {
      const mount = $("orders");
      mount.replaceChildren();
      if (!parsed.rows.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No order table found.";
        mount.appendChild(empty);
        return;
      }
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      const headerRow = document.createElement("tr");
      for (const column of parsed.columns) {
        const th = document.createElement("th");
        th.textContent = column;
        headerRow.appendChild(th);
      }
      thead.appendChild(headerRow);
      const tbody = document.createElement("tbody");
      for (const row of parsed.rows) {
        const tr = document.createElement("tr");
        if (row.status === "mid") tr.className = "mid";
        for (const column of parsed.columns) {
          const td = document.createElement("td");
          const value = row[column] || "";
          td.textContent = value;
          if (column === "side" && value === "buy") td.className = "side-buy";
          if (column === "side" && value === "sell") td.className = "side-sell";
          if (["price", "value", "size"].includes(column)) td.classList.add(column);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.append(thead, tbody);
      mount.appendChild(table);
    }

    function renderActiveGrids(grids) {
      const mount = $("activeGrids");
      mount.replaceChildren();
      if (!grids.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No active grid batches found for this wallet.";
        mount.appendChild(empty);
        return;
      }
      for (const grid of grids) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "grid-card";
        if (state.selected_grid && state.selected_grid.id === grid.id) button.classList.add("selected");
        button.dataset.gridId = grid.id;
        const title = document.createElement("div");
        title.className = "grid-card-main";
        const coin = document.createElement("span");
        coin.textContent = grid.coin || "-";
        const status = document.createElement("span");
        status.textContent = grid.status || "-";
        title.append(coin, status);
        const meta = document.createElement("div");
        meta.className = "grid-card-meta";
        const items = [
          ["limit", `${grid.limit_mode} ${grid.min_position_value || "0"}..${grid.max_position_value || "-"}`],
          ["gap", grid.gap || "-"],
          ["avg", grid.avg || "-"],
          ["trend", grid.trend || "0"],
          ["active", `B${grid.active_buy} / S${grid.active_sell}`],
          ["min", grid.min_order_value || "-"],
        ];
        for (const [key, value] of items) {
          const item = document.createElement("div");
          item.textContent = `${key}: ${value}`;
          meta.appendChild(item);
        }
        button.append(title, meta);
        mount.appendChild(button);
      }
    }

    function populateModifyForm(grid) {
      $("limitMode").value = grid.limit_mode || "abs";
      $("minPosition").value = grid.min_position_value || "0";
      $("maxPosition").value = grid.max_position_value || "";
      $("gap").value = grid.gap || "";
      $("minOrder").value = grid.min_order_value || "";
      if (grid.avg) {
        $("strategyMode").value = "avg";
        $("strategyValue").value = grid.avg;
      } else if (grid.trend && grid.trend !== "0") {
        $("strategyMode").value = "trend";
        $("strategyValue").value = grid.trend;
      } else {
        $("strategyMode").value = "keep";
        $("strategyValue").value = "";
      }
      updateModifyPreview();
    }

    function shellQuote(value) {
      const text = String(value || "");
      if (/^[A-Za-z0-9_:%+.,=-]+$/.test(text)) return text;
      return `'${text.replace(/'/g, "'\\''")}'`;
    }

    function buildModifyCommand() {
      const coin = normalizeCoin($("coin").value);
      if (!coin) return "";
      const args = [coin, "grid", "--modify"];
      const maxPosition = $("maxPosition").value.trim();
      const minPosition = $("minPosition").value.trim();
      if (maxPosition) {
        args.push(`--${$("limitMode").value}`);
        if (minPosition && minPosition !== "0") args.push(minPosition);
        args.push(maxPosition);
      }
      const gap = $("gap").value.trim();
      if (gap) args.push("--gap", gap);
      const minOrder = $("minOrder").value.trim();
      if (minOrder) args.push("--min", minOrder);
      const strategyMode = $("strategyMode").value;
      const strategyValue = $("strategyValue").value.trim();
      if (strategyMode === "avg" && strategyValue) args.push("--avg", strategyValue);
      if (strategyMode === "trend" && strategyValue) args.push("--trend", strategyValue);
      return args.map(shellQuote).join(" ");
    }

    function updateModifyPreview() {
      $("modifyCommand").textContent = buildModifyCommand() || "Pick an active grid.";
      $("applyModify").disabled = !state.selected_grid;
    }

    async function selectGrid(grid) {
      state.selected_grid = grid;
      $("coin").value = grid.coin || "";
      populateModifyForm(grid);
      renderActiveGrids(state.active_grids);
      await loadDetail();
    }

    function render(data) {
      const raw = `${data.command ? `$ ${data.command}\n\n` : ""}${data.output || ""}${data.elapsed_ms === undefined ? "" : `\n\n[${data.elapsed_ms} ms]`}`;
      $("raw").textContent = raw;
      renderSummary(parseGridSummary(data.output));
      renderOrders(parseGridOrders(data.output));
      setStatus(data.command_ok ? "Loaded" : "Error", Boolean(data.command_ok));
    }

    async function loadDetail() {
      if (!normalizeCoin($("coin").value)) return;
      try {
        setBusy(true);
        render(await apiGrid());
      } catch (error) {
        $("raw").textContent = `error: ${error.message}`;
        renderSummary([]);
        renderOrders({ columns: [], rows: [] });
        setStatus("Error", false);
      } finally {
        setBusy(false);
      }
    }

    async function refresh(event) {
      event.preventDefault();
      try {
        setBusy(true);
        const data = await apiGrids();
        state.active_grids = data.grids || [];
        const currentCoin = normalizeCoin($("coin").value);
        state.selected_grid = state.active_grids.find((grid) => grid.coin === currentCoin) || state.active_grids[0] || null;
        renderActiveGrids(state.active_grids);
        if (state.selected_grid) {
          $("coin").value = state.selected_grid.coin;
          populateModifyForm(state.selected_grid);
          await loadDetail();
        } else {
          renderSummary([]);
          renderOrders({ columns: [], rows: [] });
          $("raw").textContent = "No active grid batches found.";
        }
      } catch (error) {
        $("raw").textContent = `error: ${error.message}`;
        renderSummary([]);
        renderOrders({ columns: [], rows: [] });
        setStatus("Error", false);
      } finally {
        setBusy(false);
      }
    }

    async function applyModify(event) {
      event.preventDefault();
      if (!state.selected_grid) return;
      const command = buildModifyCommand();
      if (!command) return;
      if (!confirm(`Run modify command?\n\n${command}`)) return;
      try {
        setBusy(true);
        render(await apiRun(command));
        const grids = await apiGrids();
        state.active_grids = grids.grids || [];
        state.selected_grid = state.active_grids.find((grid) => grid.coin === normalizeCoin($("coin").value)) || state.selected_grid;
        renderActiveGrids(state.active_grids);
        if (state.selected_grid) populateModifyForm(state.selected_grid);
      } catch (error) {
        $("raw").textContent = `error: ${error.message}`;
        setStatus("Error", false);
      } finally {
        setBusy(false);
      }
    }

    function clearPage() {
      $("raw").textContent = "Ready.";
      renderSummary([]);
      renderOrders({ columns: [], rows: [] });
      state.active_grids = [];
      state.selected_grid = null;
      renderActiveGrids([]);
      $("modifyCommand").textContent = "Pick an active grid.";
      setStatus("Ready", false);
    }

    $("gridForm").addEventListener("submit", refresh);
    $("detailRefresh").addEventListener("click", () => loadDetail());
    $("modifyForm").addEventListener("submit", applyModify);
    $("clear").addEventListener("click", clearPage);
    $("activeGrids").addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) return;
      const button = event.target.closest("button[data-grid-id]");
      if (!button) return;
      const grid = state.active_grids.find((item) => item.id === button.dataset.gridId);
      if (grid) selectGrid(grid);
    });
    for (const id of ["coin", "limitMode", "maxPosition", "minPosition", "gap", "minOrder", "strategyMode", "strategyValue"]) {
      $(id).addEventListener("input", updateModifyPreview);
      $(id).addEventListener("change", updateModifyPreview);
    }
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
        <li><code>buy/sell</code>, <code>both</code>, or <code>query</code>. For ladder orders, use explicit options like <code>--for 5 -1000</code> or <code>--while 85000 +1000</code>.</li>
        <li><code>amount</code>, default <code>10</code>; use <code>--total</code> for total notional.</li>
        <li><code>entry/exec</code> options: <code>--market</code>, <code>--price</code>, <code>--offset</code>, <code>--stop</code>, <code>--stop-limit</code>, <code>--take</code>, <code>--take-limit</code>, <code>--level</code>, <code>--range</code>, <code>--for</code>, <code>--while</code>, <code>--tif</code>, <code>--slippage</code></li>
        <li><code>tp/sl</code>: <code>--tp</code>, <code>--sl</code></li>
        <li><code>--reduce-only</code></li>
      </ul>
      <p><code>both</code> creates two symmetric limit orders: buy below the center and sell above it. Use <code>--offset 2%</code> or <code>--offset 1500</code>; <code>--price</code> sets the center, otherwise current mid is used. Grid <code>--avg</code> targets a position value; far-side topups keep size fixed and widen the risk-adding side's gap with an asymptotic multiplier, and it is mutually exclusive with <code>--trend</code>. <code>--stop</code> / <code>--take</code> are entry triggers. <code>--stop</code> is breakout style and <code>--take</code> is if-touched style. You can append <code>+50</code>, <code>-50</code>, <code>+0.2%</code>, or <code>-0.2%</code> to set the post-trigger limit price; without a suffix the order is a market trigger. <code>--stop-limit</code> / <code>--take-limit</code> still work as explicit equivalents. Percent is literal: <code>70000+2%</code> means <code>71400</code>, while <code>70140</code> is <code>70000+0.2%</code>. These trigger-limit styles are not <code>ALO</code>; only plain limit orders use <code>--tif</code>. <code>--tp</code> / <code>--sl</code> also accept absolute prices, absolute prices plus offsets, or relative percentages from the entry / position price such as <code>2%+0.1%</code>. Unsigned TP/SL percentages auto-follow side: long TP up and SL down, short TP down and SL up. You can append <code>d0.6</code> or <code>d60%</code> to close only part of the order. <code>--for COUNT STEP</code> is a count ladder, <code>--while END STEP</code> is a range ladder, and <code>--while END --for COUNT</code> auto-calculates the step so the ladder has exactly <code>COUNT</code> legs. <code>--range START END STEP</code> is shorthand for <code>--price START --while END STEP</code>; <code>--range START END --for COUNT</code> auto-calculates the step from explicit start to end. <code>--total</code> divides total notional across ladder or symmetric legs. Use <code>--explain</code> to print the parsed plan without submitting. Ordinary ladders can also carry <code>--tp</code> / <code>--sl</code>, with each ladder leg getting its own bracket. Trigger ladders can also use <code>--stop</code> / <code>--take</code> so each leg gets its own trigger anchor. That trigger-ladder mode can work with <code>--reduce-only</code>, but it cannot share the same submit with <code>--tp</code> / <code>--sl</code> because <code>normalTpsl</code> requires a non-trigger main order. It is different from <code>--scale</code>, which splits a total amount evenly. <code>--level</code> sets the same-side book depth.</p>
    </section>
    <section>
      <h2>Flow</h2>
      <p>The server reads the wallet address and private key or agent key from its local environment. Tap Verify to run a server-side query before sending commands.</p>
    </section>
    <section>
      <h2>Examples</h2>
      <ul>
        <li><code>BTC</code></li>
        <li><code>query</code></li>
        <li><code>BTC buy 10 --dry-run</code></li>
        <li><code>BTC buy 10 --market --dry-run</code></li>
        <li><code>BTC buy 10 --price 75000 --dry-run</code></li>
        <li><code>BTC both 100 --price 75000 --offset 2% --dry-run</code></li>
        <li><code>BTC both --total 200 --offset 2% --explain</code></li>
        <li><code>JPY both 20 --offset 2% --tp 1% --sl 0.7% --explain</code></li>
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
        <li><code>BTC sell --tp 2%+0.1% --sl 2%-0.1% --reduce-only --dry-run</code></li>
        <li><code>BTC buy --tp 2%+0.1% --sl 2%-0.1% --dry-run</code></li>
        <li><code>BTC sell --tp 2%-0.1% --sl 2%+0.1% --dry-run</code></li>
        <li><code>BTC buy 100 --price 68000 --tp 72000 --sl 65000 --dry-run</code></li>
        <li><code>BTC buy 30 --stop 80000-10 --tp 0.6%+0d0.6 --dry-run</code></li>
        <li><code>BTC buy 100 --scale 5 --from 67000 --to 63000 --dry-run</code></li>
        <li><code>BTC buy --for 5 -1000 --price 67000 --dry-run</code></li>
        <li><code>BTC sell --while 85000 +1000 --price 80000 --dry-run</code></li>
        <li><code>HYPE buy --total 50 --while 68 --for 3 --tp 0.07%+0.02d0.9 --explain</code></li>
        <li><code>HYPE buy --total 50 --range 68.5 68 --for 3 --tp 0.07%+0.02d0.9 --explain</code></li>
        <li><code>BTC sell 10 --while 80000 +1000 --stop 77000 --reduce-only --dry-run</code></li>
        <li><code>BTC buy --for 5 -1000 --price 67000 --tp 5%+0 --sl 2%-10 --dry-run</code></li>
        <li><code>HYPE buy --total 120 --range 66 65 -0.05 --tp 0.07%+0.01d0.9 --explain</code></li>
        <li><code>BTC --cancel --dry-run</code></li>
        <li><code>BTC --cancel up --dry-run</code></li>
        <li><code>BTC --cancel down --dry-run</code></li>
        <li><code>BTC --cancel up --price 80000 --dry-run</code></li>
        <li><code>BTC --cancel down --price 75000 --dry-run</code></li>
        <li><code>BTC --cancel buy --dry-run</code></li>
        <li><code>BTC --cancel sell --dry-run</code></li>
        <li><code>BTC --cancel tp --dry-run</code></li>
        <li><code>BTC --cancel sl --dry-run</code></li>
        <li><code>BTC --cancel hour --dry-run</code></li>
        <li><code>BTC --cancel day --dry-run</code></li>
        <li><code>BTC --cancel week --dry-run</code></li>
        <li><code>BTC --cancel hour --range 3 5 --dry-run</code></li>
        <li><code>BTC --cancel hour --range 3 --dry-run</code></li>
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
        <li><code>--cancel</code> cancels all open orders for the coin by default. Use <code>--cancel up</code> / <code>down</code> for orders above / below the current mid, add <code>--price</code> to use a fixed threshold, use <code>buy</code> / <code>sell</code> for side filters, <code>tp</code> / <code>sl</code> for take-profit / stop-loss trigger filters, use <code>hour</code> / <code>day</code> / <code>week</code> for orders older than 1 unit by default, add <code>--range 3 5</code> for an age range or <code>--range 3</code> for age 3+ units, or pass an OID.</li>
        <li>Market orders use <code>--market</code> and Hyperliquid IOC behavior.</li>
        <li><code>--stop-entry</code> or <code>--stop</code> creates a breakout-style entry trigger. Without a suffix it becomes a market trigger; with a suffix such as <code>+50</code> or <code>+0.2%</code> it becomes a trigger-limit order.</li>
        <li><code>--take-entry</code> or <code>--take</code> creates an if-touched entry trigger. Without a suffix it becomes a market trigger; with a suffix such as <code>+50</code> or <code>+0.2%</code> it becomes a trigger-limit order.</li>
        <li>Percent offsets are literal: <code>70000+2%</code> means <code>71400</code>, while <code>70140</code> is <code>70000+0.2%</code>.</li>
        <li>Percentage-derived trigger and limit prices are snapped to the exchange's accepted price precision before submission.</li>
        <li>Trigger-limit orders are not <code>ALO</code>; only plain limit orders use <code>--tif</code>.</li>
        <li><code>--tp</code> / <code>--sl</code> can use absolute prices, absolute prices plus offsets, or relative percentages from the entry / position price such as <code>2%+0.1%</code>. Unsigned percentages auto-follow side; explicit <code>+</code> / <code>-</code> still override.</li>
        <li><code>--tp</code> / <code>--sl</code> without <code>--reduce-only</code> create a bracket order; with <code>--reduce-only</code> they create protective position TP/SL orders.</li>
        <li><code>both</code> requires <code>--offset</code>. Positional <code>amount</code> is per side; <code>--total</code> splits evenly across the buy and sell legs. It can carry <code>--tp</code> / <code>--sl</code>; the buy leg uses long TP/SL direction and the sell leg uses short TP/SL direction. Actual notional may be slightly higher after exchange size rounding and current-mid minimum-value checks.</li>
        <li><code>--for COUNT STEP</code> is a count ladder, <code>--while END STEP</code> is a range ladder, and <code>--while END --for COUNT</code> auto-calculates the step so the ladder has exactly <code>COUNT</code> legs. <code>--range START END STEP</code> is shorthand for <code>--price START --while END STEP</code>; <code>--range START END --for COUNT</code> auto-calculates the step from explicit start to end. Use <code>--total</code> to divide total notional across ladder legs, and <code>--explain</code> to print the parsed plan without submitting. Ordinary ladders can also carry <code>--tp</code> / <code>--sl</code>, with each ladder leg getting its own bracket. Trigger ladders can also use <code>--stop</code> / <code>--take</code> so each leg gets its own trigger anchor. That trigger-ladder mode can work with <code>--reduce-only</code>, but it cannot share the same submit with <code>--tp</code> / <code>--sl</code> because <code>normalTpsl</code> requires a non-trigger main order. It is different from <code>--scale</code>, which splits a total amount evenly.</li>
        <li>The web page shows the same local timestamps as the terminal.</li>
        <li><code>--level</code> sets the same-side book depth.</li>
        <li><code>--scale</code> splits a total USD amount into multiple limit orders.</li>
        <li>Command history is saved on the server, so the same history appears across devices using this server.</li>
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


def mask_account(account_address: str) -> str:
    return f"{account_address[:6]}...{account_address[-4:]}" if account_address else ""


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


def normalize_history_command(raw: Any) -> str:
    command = str(raw or "").strip()
    if not command:
        return ""
    command = " ".join(command.splitlines()).strip()
    if len(command) > MAX_COMMAND_LENGTH:
        raise ValueError("command is too long")
    return command


def normalize_history_items(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    history: list[str] = []
    seen: set[str] = set()
    for item in items:
        command = normalize_history_command(item)
        if not command or command in seen:
            continue
        seen.add(command)
        history.append(command)
        if len(history) >= COMMAND_HISTORY_LIMIT:
            break
    return history


def load_command_history() -> list[str]:
    with COMMAND_HISTORY_LOCK:
        try:
            raw = json.loads(COMMAND_HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("history")
        return normalize_history_items(raw)


def save_command_history(history: list[str]) -> None:
    with COMMAND_HISTORY_LOCK:
        COMMAND_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"history": normalize_history_items(history), "updated_at": int(time.time())}
        tmp_path = COMMAND_HISTORY_PATH.with_name(f".{COMMAND_HISTORY_PATH.name}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, COMMAND_HISTORY_PATH)


def append_command_history(command: Any) -> list[str]:
    normalized = normalize_history_command(command)
    history = load_command_history()
    if normalized:
        history = [item for item in history if item != normalized]
        history.insert(0, normalized)
        history = history[:COMMAND_HISTORY_LIMIT]
        save_command_history(history)
    return history


def load_server_credentials(env_path: Path = PROJECT_DIR / ".env") -> tuple[str, str]:
    values: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip().strip('"').strip("'")

    account_address = values.get("account_address", "")
    secret_key = values.get("secret_key", "")
    for key in ("account_address", "secret_key"):
        for env_key in (key, key.upper()):
            env_value = os.environ.get(env_key)
            if not env_value:
                continue
            if key == "account_address":
                account_address = env_value.strip()
            else:
                secret_key = env_value.strip()
            break

    if not ADDRESS_RE.fullmatch(account_address):
        raise ValueError("invalid server wallet address")
    if not SECRET_RE.fullmatch(secret_key):
        raise ValueError("invalid server key")
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


def parse_grid_coin(raw: Any) -> str:
    coin = str(raw or "").strip().upper()
    if not coin:
        raise ValueError("coin is required")
    if len(coin) > 24 or not re.fullmatch(r"[A-Z0-9:_-]+", coin):
        raise ValueError("invalid coin")
    return coin


def load_grid_batch_rows(account_address: str, network: str = "mainnet") -> list[dict[str, Any]]:
    path = PROJECT_DIR / "server_batch.json"
    if not path.exists():
        return []
    try:
        raw_rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw_rows, list):
        return []
    account = account_address.lower()
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "grid":
            continue
        if str(row.get("status", "")) not in {"active", "error"}:
            continue
        if str(row.get("network", "mainnet")) != network:
            continue
        if str(row.get("account", "")).lower() != account:
            continue
        rows.append(row)
    rows.sort(key=lambda row: int(row.get("updated_at") or row.get("created_at") or 0), reverse=True)
    return rows


def summarize_grid_row(row: dict[str, Any]) -> dict[str, Any]:
    levels = [item for item in row.get("levels") or [] if isinstance(item, dict)]
    live_statuses = {
        "active",
        "pending",
        "paused_max",
        "paused_limit",
        "paused_margin",
        "paused_reduce_capacity",
        "paused_account_margin",
        "paused_roe",
    }
    active_buy = len([item for item in levels if item.get("side") == "buy" and str(item.get("status", "active")) in live_statuses])
    active_sell = len([item for item in levels if item.get("side") == "sell" and str(item.get("status", "active")) in live_statuses])
    return {
        "id": str(row.get("id", "")),
        "coin": str(row.get("coin", "")),
        "status": str(row.get("status", "")),
        "network": str(row.get("network", "")),
        "dex": str(row.get("dex", "")),
        "limit_mode": str(row.get("position_limit_mode", "abs")),
        "min_position_value": str(row.get("min_position_value", "0")),
        "max_position_value": str(row.get("max_position_value", "")),
        "min_order_value": str(row.get("min_order_value", "")),
        "gap": str(row.get("gap", "")),
        "gap_rate": str(row.get("gap_rate", "")),
        "avg": "" if row.get("avg") is None else str(row.get("avg", "")),
        "trend": str(row.get("trend", "0")),
        "active_buy": active_buy,
        "active_sell": active_sell,
        "level_count": len(levels),
        "updated_at": int(row.get("updated_at") or row.get("created_at") or 0),
        "note": str(row.get("note", "")),
    }


def list_grid_batches(payload: dict[str, Any], account_address: str) -> dict[str, Any]:
    network = str(payload.get("network") or "mainnet")
    if network not in {"mainnet", "testnet"}:
        raise ValueError("invalid network")
    grids = [summarize_grid_row(row) for row in load_grid_batch_rows(account_address, network)]
    return {"ok": True, "grids": grids, "count": len(grids), "network": network}


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


def run_grid_query(payload: dict[str, Any], account_address: str, secret_key: str) -> dict[str, Any]:
    coin = parse_grid_coin(payload.get("coin"))
    return run_hl_order([coin, "grid", "--query"], account_address, secret_key)


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
        if path == "/grid":
            self.send_payload(HTTPStatus.OK, GRID_HTML.encode("utf-8"), "text/html; charset=utf-8")
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
        if path == "/grid":
            self.send_headers(HTTPStatus.OK, len(GRID_HTML.encode("utf-8")), "text/html; charset=utf-8")
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
            if path not in {"/api/run", "/api/grid", "/api/grids", "/api/history", "/api/verify"}:
                self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            payload = parse_json_body(self)
            if path == "/api/history":
                command = payload.get("command")
                history = append_command_history(command) if command is not None else load_command_history()
                self.send_json({"ok": True, "history": history, "count": len(history)})
                return
            if path == "/api/verify":
                account_address, _secret_key = load_server_credentials()
                self.send_json({"ok": True, "account": mask_account(account_address)})
                return
            account_address, secret_key = load_server_credentials()
            if path == "/api/grids":
                self.send_json(list_grid_batches(payload, account_address))
                return
            if path == "/api/grid":
                self.send_json(run_grid_query(payload, account_address, secret_key))
                return
            args = parse_command(payload.get("command"))
            self.send_json(run_hl_order(args, account_address, secret_key))
        except subprocess.TimeoutExpired:
            self.send_json(
                {"ok": False, "error": f"command timed out after {COMMAND_TIMEOUT:g}s"},
                HTTPStatus.REQUEST_TIMEOUT,
            )
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
