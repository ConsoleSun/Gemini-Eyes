#!/usr/bin/env node
// Click absolute viewport coordinates with real CDP mouse events.
// Usage: node cdp_click_at.mjs <x> <y> [port]
const x = Number(process.argv[2]); const y = Number(process.argv[3]); const port = process.argv[4] ?? "9333";
const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
const ws = new WebSocket(version.webSocketDebuggerUrl);
const pending = new Map(); let nextId = 1;
const send = (method, params = {}, sessionId) => new Promise((res, rej) => { const id = nextId++; pending.set(id, m => (m.error ? rej(new Error(m.error.message)) : res(m.result))); ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) })); });
ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
await new Promise(r => ws.addEventListener("open", r, { once: true }));
const { targetInfos } = await send("Target.getTargets");
const page = targetInfos.find(t => t.type === "page" && t.url.includes("gemini.google.com")) ?? targetInfos.find(t => t.type === "page");
const { sessionId } = await send("Target.attachToTarget", { targetId: page.targetId, flatten: true });
for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 }, sessionId);
console.log(JSON.stringify({ clickedAt: [x, y] }));
ws.close();