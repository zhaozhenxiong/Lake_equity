import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { datasetIds, ds, dsWithCode, getLang, setLang, tr } from "../src/i18n.js";

test("every registered dataset has a Chinese and English name", () => {
  const source = readFileSync(new URL("../../src/cnequity/domain/datasets.py", import.meta.url), "utf8");
  const registered = [...source.matchAll(/DatasetSpec\(\s*\n\s*"([a-z0-9_]+)"/g)].map((match) => match[1]);
  assert.ok(registered.length > 40);
  const known = new Set(datasetIds());
  const missing = registered.filter((name) => !known.has(name));
  assert.deepEqual(missing, []);
});

test("the language switch changes dataset titles and page text", () => {
  setLang("zh");
  assert.equal(getLang(), "zh");
  assert.equal(ds("daily_bars"), "日线");
  assert.equal(dsWithCode("daily_bars"), "日线 · daily_bars");
  assert.equal(tr("数据集", "Datasets"), "数据集");
  setLang("en");
  assert.equal(ds("daily_bars"), "Daily bars");
  assert.equal(ds("not_a_dataset"), "not_a_dataset");
  assert.equal(tr("数据集", "Datasets"), "Datasets");
  setLang("zh");
});

test("with no stored choice the page follows the browser's language", async () => {
  const { browserLang } = await import("../src/i18n.js");
  assert.equal(browserLang(["zh-CN", "en"]), "zh");
  assert.equal(browserLang(["zh-Hans"]), "zh");
  assert.equal(browserLang(["zh"]), "zh");
  assert.equal(browserLang(["en-US", "zh-CN"]), "en");
  assert.equal(browserLang(["zh-TW"]), "en");
  assert.equal(browserLang(["fr"]), "en");
  assert.equal(browserLang([]), "en");
  assert.equal(browserLang(null), "en");
});
