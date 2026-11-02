#!/usr/bin/env node
// Dump the Gemini session out of the dedicated Edge profile over CDP.
//
// Why: Chromium on Windows encrypts cookies with app-bound keys (v20), so the
// sqlite file cannot be decrypted from outside the browser. The browser itself
// can, so we ask it through the DevTools protocol.
//
// Usage: node dump_cookies_cdp.mjs [port] [outfile]

import { writeFileSync } from "node:fs";

const port = process.argv[2] ?? "9333";
const outfile = process.argv[3] ?? "cookies.json";

const versionUrl = `http://127.0.0.1:${port}/json/version`;

async function main() {
  const version = await fetch(versionUrl).then((r) => r.json());
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  const pending = new Map();
  let nextId = 1;

  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, (message) =>
        message.error ? reject(new Error(message.error.message)) : resolve(message.result)
      );
      ws.send(JSON.stringify({ id, method, params }));
    });

  await new Promise((resolve) => ws.addEventListener("open", resolve, { once: true }));
  const { cookies } = await send("Storage.getCookies");
  ws.close();

  const jar = {};
  for (const cookie of cookies) {
    if (!cookie.domain.endsWith("google.com")) continue;
    if (!cookie.value) continue;
    // Prefer the widest scope so one entry per name survives.
    if (jar[cookie.name] && cookie.domain !== ".google.com") continue;
    jar[cookie.name] = cookie.value;
  }
  writeFileSync(outfile, JSON.stringify(jar, null, 2), "utf8");
  const critical = ["SID", "__Secure-1PSID", "__Secure-1PSIDTS"].filter((k) => !jar[k]);
  console.log(`cookies: ${Object.keys(jar).length}`);
  console.log(`critical missing: ${critical.length ? critical.join(", ") : "none"}`);
  if (critical.length) process.exitCode = 2;
}

main().catch((error) => {
  console.error(`CDP dump failed: ${error.message}`);
  process.exitCode = 1;
});