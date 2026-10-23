import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";

// Every Chinese string the console shows must also exist in English, which in
// this code base means it sits inside `tr(zh, en)`. The scanner walks real JS
// tokens (strings, templates, regexes, comments) rather than lines, so a
// multi-line `tr(` call is recognised and a regex such as `/版$/` is not text.
//
// Exempt: the paired-name tables in i18n.js and runs-model.js (covered by
// i18n.test.js), and any line ending in `// i18n-ignore` — strings the code
// matches against server text rather than shows.

const SRC = new URL("../src/", import.meta.url);
const PAIR_TABLES = new Set(["i18n.js", "runs-model.js"]);
const CJK = /[一-鿿]/;
const REGEX_PREV = new Set(["(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-", "*", "%", "<", ">", "~", "^", ""]);

export function untranslated(source) {
  const found = [];
  const calls = []; // innermost-last: callee name for "(", "${" for template holes
  const templates = []; // depth of `calls` at which each open template's hole started
  let i = 0;
  let line = 1;
  let lastSignificant = "";
  const ignored = new Set(
    source.split("\n").flatMap((text, index) => (text.trimEnd().endsWith("// i18n-ignore") ? [index + 1] : [])),
  );

  const insideTr = () => {
    for (let k = calls.length - 1; k >= 0; k -= 1) {
      if (calls[k] === "${") continue;
      return calls[k] === "tr";
    }
    return false;
  };
  const report = (text, at) => {
    if (CJK.test(text) && !insideTr() && !ignored.has(at)) found.push({ line: at, text: text.slice(0, 80) });
  };
  const calleeBefore = (pos) => {
    let j = pos - 1;
    while (j >= 0 && /\s/.test(source[j])) j -= 1;
    let end = j + 1;
    while (j >= 0 && /[\w$.]/.test(source[j])) j -= 1;
    const name = source.slice(j + 1, end);
    return name.split(".").pop();
  };

  // Reads a template literal body starting after the opening backtick; stops at
  // the closing backtick or at a `${` hole (pushing it) and returns the index.
  const readTemplate = (start) => {
    let j = start;
    let chunk = "";
    const at = line;
    while (j < source.length) {
      const ch = source[j];
      if (ch === "\\") {
        chunk += source.slice(j, j + 2);
        j += 2;
        continue;
      }
      if (ch === "\n") line += 1;
      if (ch === "`") {
        report(chunk, at);
        templates.pop();
        return j + 1;
      }
      if (ch === "$" && source[j + 1] === "{") {
        report(chunk, at);
        calls.push("${");
        return j + 2;
      }
      chunk += ch;
      j += 1;
    }
    return j;
  };

  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === "\n") {
      line += 1;
      i += 1;
      continue;
    }
    if (/\s/.test(ch)) {
      i += 1;
      continue;
    }
    if (ch === "/" && next === "/") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      const end = source.indexOf("*/", i + 2);
      line += (source.slice(i, end).match(/\n/g) || []).length;
      i = end + 2;
      continue;
    }
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < source.length && source[j] !== ch) j += source[j] === "\\" ? 2 : 1;
      report(source.slice(i + 1, j), line);
      i = j + 1;
      lastSignificant = "str";
      continue;
    }
    if (ch === "`") {
      templates.push(calls.length);
      i = readTemplate(i + 1);
      lastSignificant = "str";
      continue;
    }
    if (ch === "/" && REGEX_PREV.has(lastSignificant)) {
      let j = i + 1;
      let inClass = false;
      while (j < source.length && (inClass || source[j] !== "/")) {
        if (source[j] === "\\") j += 1;
        else if (source[j] === "[") inClass = true;
        else if (source[j] === "]") inClass = false;
        j += 1;
      }
      i = j + 1;
      while (/[a-z]/.test(source[i] || "")) i += 1;
      lastSignificant = "regex";
      continue;
    }
    if (ch === "(") {
      calls.push(calleeBefore(i));
    } else if (ch === ")") {
      calls.pop();
    } else if (ch === "{") {
      calls.push("{");
    } else if (ch === "}") {
      const top = calls.pop();
      if (top === "${") {
        // Back inside the template that opened this hole.
        i = readTemplate(i + 1);
        lastSignificant = "str";
        continue;
      }
    }
    lastSignificant = /[\w$]/.test(ch) ? "id" : ch;
    i += 1;
  }
  return found;
}

test("every Chinese string in the console has an English counterpart", () => {
  const problems = [];
  for (const name of readdirSync(SRC).filter((file) => file.endsWith(".js"))) {
    if (PAIR_TABLES.has(name)) continue;
    for (const hit of untranslated(readFileSync(new URL(name, SRC), "utf8"))) {
      problems.push(`${name}:${hit.line} ${hit.text}`);
    }
  }
  assert.deepEqual(problems, []);
});

test("the scanner tells tr() text from plain text", () => {
  assert.deepEqual(untranslated('x = tr(`已在${a}重建`, `Rebuilt ${a}`);'), []);
  assert.deepEqual(untranslated('x = tr(\n  "多行",\n  "multi",\n);'), []);
  assert.deepEqual(untranslated("s.replace(/版$/, '')"), []);
  assert.deepEqual(untranslated('if (m.includes("尚未配置")) {} // i18n-ignore'), []);
  assert.equal(untranslated('el.textContent = "已安装。";').length, 1);
  assert.equal(untranslated('html = `<h2>${esc(t)}研究包</h2>`;').length, 1);
});
