#!/usr/bin/env node
// Evaluate a JS expression in the dedicated profile page over CDP.
// Usage: node cdp_eval.mjs "<expression>" [port]
import { readFileSync } from "node:fs";

const expression = process.argv[2];
const port = process.argv[3] ?? "9333";
if (!expression) {
  console.error("usage: node cdp_eval.mjs <expression> [port]");
  process.exit(2);
}

const version = await fetch(`http://127.0.0.1:${port}/json/version`).then((r) => r.json());
const ws = new WebSocket(version.webSocketDebuggerUrl);
const pending = new Map();
let nextId = 1;
const send = (method, params = {}, sessionId) =>
  new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, (m) => (m.error ? reject(new Error(m.error.message)) : resolve(m.result)));
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });

ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
await new Promise((r) => ws.addEventListener("open", r, { once: true }));

const { targetInfos } = await send("Target.getTargets");
const page = targetInfos.find((t) => t.type === "page" && t.url.includes("gemini.google.com"))
  ?? targetInfos.find((t) => t.type === "page");
const { sessionId } = await send("Target.attachToTarget", { targetId: page.targetId, flatten: true });

const result = await send("Runtime.evaluate", {
  expression,
  returnByValue: true,
  awaitPromise: true,
}, sessionId);
if (result.exceptionDetails) {
  console.error(JSON.stringify(result.exceptionDetails, null, 2));
  process.exitCode = 1;
} else {
  console.log(JSON.stringify(result.result.value, null, 2));
}
ws.close();