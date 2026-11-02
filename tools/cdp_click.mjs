#!/usr/bin/env node
// Click an element by its exact visible text using real CDP mouse events.
// Usage: node cdp_click.mjs "<exact text>" [port]
const text = process.argv[2];
const port = process.argv[3] ?? "9333";
if (!text) { console.error("usage: node cdp_click.mjs <text> [port]"); process.exit(2); }

const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
const ws = new WebSocket(version.webSocketDebuggerUrl);
const pending = new Map(); let nextId = 1;
const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
  const id = nextId++;
  pending.set(id, m => (m.error ? rej(new Error(m.error.message)) : res(m.result)));
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});
ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
await new Promise(r => ws.addEventListener("open", r, { once: true }));

const { targetInfos } = await send("Target.getTargets");
const page = targetInfos.find(t => t.type === "page" && t.url.includes("gemini.google.com")) ?? targetInfos.find(t => t.type === "page");
const { sessionId } = await send("Target.attachToTarget", { targetId: page.targetId, flatten: true });

const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sessionId);
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception?.description ?? r.exceptionDetails));
  return r.result.value;
};

const rect = await ev(`(() => {
  const t = ${JSON.stringify(text)};
  const els = [...document.querySelectorAll('*')].filter(el => ((el.innerText || el.textContent) || '').trim() === t && el.offsetParent !== null);
  if (!els.length) return null;
  const el = els[els.length - 1];
  const r = el.getBoundingClientRect();
  return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
})()`);
if (!rect) { console.error(`not found: ${text}`); process.exit(3); }

for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
  await send("Input.dispatchMouseEvent", { type, x: rect.x, y: rect.y, button: "left", clickCount: 1 }, sessionId);
}
console.log(JSON.stringify({ clicked: text, ...rect }));
ws.close();