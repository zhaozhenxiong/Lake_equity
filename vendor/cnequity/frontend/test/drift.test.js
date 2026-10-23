import assert from "node:assert/strict";
import test from "node:test";
import { configDriftBanner, distinctTails, driftHeldNote, restartCommand } from "../src/drift.js";

const esc = (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
const base = { data_root: "/home/me/lakes/lake-a", config_path: "/home/me/cfg/cnequity.toml" };

test("no drift, no banner and no held note", () => {
  assert.equal(configDriftBanner({ ...base, config_drift: null }, esc), "");
  assert.equal(configDriftBanner(undefined, esc), "");
  assert.equal(driftHeldNote({ mode: { ...base, config_drift: null } }, "x"), "");
});

test("paths are cut to what differs, with the whole path kept as a tooltip", () => {
  assert.deepEqual(distinctTails("/home/me/lakes/lake-a", "/home/me/lakes/lake-b"), ["…/lake-a", "…/lake-b"]);
  assert.deepEqual(distinctTails("D:\\lakes\\a", "D:\\lakes\\b"), ["…\\a", "…\\b"]);
  assert.deepEqual(distinctTails("/a", "/b"), ["/a", "/b"]);
  const html = configDriftBanner({ ...base, config_drift: { kind: "data_root", configured_data_root: "/home/me/lakes/lake-<b>" } }, esc);
  assert.match(html, /title="\/home\/me\/lakes\/lake-a">…\/lake-a</);
  assert.match(html, /…\/lake-&lt;b>/);
  assert.match(html, /data-copy-command="cne serve --config \/home\/me\/cfg\/cnequity.toml"/);
});

test("the restart command quotes a path that needs it", () => {
  assert.equal(restartCommand("/srv/cne.toml"), "cne serve --config /srv/cne.toml");
  assert.equal(restartCommand("C:\\Users\\me\\My Lake\\cne.toml"), 'cne serve --config "C:\\Users\\me\\My Lake\\cne.toml"');
  assert.equal(restartCommand("/数据/cne.toml"), 'cne serve --config "/数据/cne.toml"');
});

test("an unreadable file says where it broke", () => {
  const invalid = configDriftBanner({ ...base, config_drift: { kind: "invalid", line: 3, column: 6, detail: "Expected ']'" } }, esc);
  assert.match(invalid, /drift-broken/);
  assert.match(invalid, /3/);
  assert.match(invalid, /6/);
  assert.match(invalid, /Expected '\]'/);
  assert.match(invalid, /cnequity\.toml/);
  assert.doesNotMatch(invalid, /data-copy-command/);
  assert.match(configDriftBanner({ ...base, config_drift: { kind: "missing" } }, esc), /drift-broken/);
});

test("settings panels are held with a note instead of an error", () => {
  const note = driftHeldNote({ mode: { ...base, config_drift: { kind: "data_root", configured_data_root: "/x" } } }, "T");
  assert.match(note, /<h2>T<\/h2>/);
  assert.match(note, /cne serve/);
  assert.doesNotMatch(note, /err/);
});
