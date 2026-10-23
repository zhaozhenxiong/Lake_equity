import assert from "node:assert/strict";
import test from "node:test";
import { runLauncherModel } from "../src/ops.js";
import {
  ATTENTION_STATUSES,
  failurePreview,
  jobTitle,
  openBatchFailures,
  parseRunsHash,
  presetsEqual,
  runsHref,
  runsListPath,
  runsStatusParam,
} from "../src/runs-model.js";

const line = (message) => String(message || "").split("\n")[0];

test("run filters and launch presets survive paging", () => {
  const state = parseRunsHash("#/runs?view=attention&offset=100&op=daily.full&trade_date=2026-10-01");
  assert.equal(state.view, "attention");
  assert.equal(state.offset, 100);
  assert.equal(state.op, "daily.full");
  assert.equal(state.preset.get("trade_date"), "2026-10-01");
  assert.equal(runsStatusParam(state.view), ATTENTION_STATUSES);
  assert.equal(
    runsHref({ offset: 200 }, state),
    "#/runs?view=attention&offset=200&op=daily.full&trade_date=2026-10-01",
  );
  assert.equal(runsHref({ view: "all", offset: 0 }, state), "#/runs?op=daily.full&trade_date=2026-10-01");
  assert.equal(runsHref({ op: "", preset: new URLSearchParams() }, state), "#/runs?view=attention&offset=100");
  assert.equal(parseRunsHash("#/runs?view=nope").view, "all");
  assert.equal(presetsEqual(state.preset, { trade_date: "2026-10-01" }), true);
  assert.equal(presetsEqual(state.preset, {}), false);
});

test("attention lists only runs that are still open", () => {
  assert.equal(runsListPath("attention", 0, 1), "/api/runs?limit=1&offset=0&open=1");
  assert.equal(runsListPath("running", 0, 1), "/api/runs?limit=1&offset=0&status=running");
  assert.equal(runsListPath("all", 0, 100), "/api/runs?limit=100&offset=0");
  assert.equal(runsStatusParam("attention"), ATTENTION_STATUSES);
});

test("a handled or outdated batch is not a failure to count", () => {
  const open = openBatchFailures([
    { status: "failed", error_message: "still open" },
    { status: "superseded", error_message: "superseded by successful retry batch x" },
    { status: "success", error_message: "recovered" },
    { status: "stale", error_message: "silent too long" },
    { status: "failed", error_message: "" },
  ]);
  assert.deepEqual(
    open.map((batch) => batch.error_message),
    ["still open"],
  );
});

test("job titles name the family and keep a catch-up distinct from a daily run", () => {
  assert.equal(jobTitle("daily:core"), "日更");
  assert.equal(jobTitle("daily:stale"), "补抓");
  assert.equal(jobTitle("derive:adj_factors"), "派生");
  assert.equal(jobTitle("init"), "初始化");
  assert.equal(jobTitle("custom-job"), "custom-job");
});

test("the list keeps two failure lines and does not repeat the generic run error", () => {
  const preview = failurePreview(
    {
      error_message: "one or more core steps failed",
      failures: [
        { dataset: "a", error_message: "first\ntrailer" },
        { dataset: "b", error_message: "second" },
        { dataset: "c", error_message: "third" },
      ],
    },
    line,
  );
  assert.deepEqual(preview.shown.map((item) => item.text), ["first", "second"]);
  assert.equal(preview.extra, 1);
  assert.deepEqual(failurePreview({ error_message: "source cooled down", failures: [] }, line).shown, [
    { dataset: "", text: "source cooled down" },
  ]);
});

test("a closed session offers the pending date instead of run-today", () => {
  const home = {
    mode: { ops_enabled: true },
    operations: ["daily.full", "daily.group", "daily.stale", "events.run", "backfill.run", "derive.run"].map((id) => ({
      id,
      available: true,
      summary: id,
    })),
    occupancy: { schedule: { today_is_session: false, daily_pending: "2026-10-02", today: "2026-10-04" } },
  };
  const open = runLauncherModel(home);
  assert.equal(open.items[0].id, "daily.full");
  assert.deepEqual(open.items[0].preset, { trade_date: "2026-10-02" });
  assert.equal(open.items[0].disabled, false);
  assert.equal(open.closedNote, "");

  home.occupancy.schedule.daily_pending = "";
  const closed = runLauncherModel(home);
  assert.equal(closed.items.some((item) => item.id === "daily.full"), false);
  assert.match(closed.closedNote, /不是交易日/);
  assert.deepEqual(
    closed.items.map((item) => item.id),
    ["daily.group", "daily.stale", "events.run", "backfill.run", "derive.run"],
  );
});

test("manual run buttons stay disabled when the panel cannot start commands", () => {
  const model = runLauncherModel({
    mode: { ops_enabled: false },
    operations: [{ id: "daily.full", available: true, summary: "daily" }],
    occupancy: { schedule: { today_is_session: true } },
  });
  assert.equal(model.items.every((item) => item.disabled), true);
  assert.match(model.items[0].reason, /不能从面板启动/);
});
