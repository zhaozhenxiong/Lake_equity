import assert from "node:assert/strict";
import test from "node:test";
import { beginOps, closeOps, mountOpForm } from "../src/ops.js";

const DERIVE = {
  id: "derive.run",
  title: "重算派生数据",
  summary: "",
  params: [{ name: "name", kind: "choice", label: "派生", choices: ["adj_factors", "minute_bars_15m"] }],
};

function mount(t, query) {
  const saved = Object.getOwnPropertyDescriptor(globalThis, "document");
  globalThis.document = { getElementById: () => null };
  t.after(() => {
    closeOps();
    if (saved) Object.defineProperty(globalThis, "document", saved);
    else delete globalThis.document;
  });
  const host = { innerHTML: "" };
  const home = { operations: [DERIVE], occupancy: {}, mode: { ops_enabled: true } };
  mountOpForm({ esc: (value) => String(value ?? "") }, home, "derive.run", new URLSearchParams(query), host, beginOps());
  return host.innerHTML;
}

test("choosing a minute resample explains that it is opt-in and how it builds", (t) => {
  const html = mount(t, "name=minute_bars_15m");
  assert.match(html, /默认不计算/);
  assert.match(html, /resampled_from/);
  assert.match(html, /15 分钟线 · minute_bars_15m/);
});

test("other derive targets keep the form free of the minute note", (t) => {
  const html = mount(t, "name=adj_factors");
  assert.doesNotMatch(html, /默认不计算/);
});
