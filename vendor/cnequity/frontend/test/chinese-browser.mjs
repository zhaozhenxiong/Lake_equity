// The page now follows the browser's language when nothing is stored, and
// Node's own `navigator` reports en-US. These tests' fixtures and assertions
// are written against the Chinese page, so they run as a Chinese browser; the
// browser-language rule itself is tested with explicit tag lists.
Object.defineProperty(globalThis, "navigator", {
  value: { language: "zh-CN", languages: ["zh-CN"] },
  configurable: true,
});
