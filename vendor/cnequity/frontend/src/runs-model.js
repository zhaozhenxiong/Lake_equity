// Hash, labels, and failure lines for the runs page. No DOM and no charts.
import { tr } from "./i18n.js";

const VIEWS = new Set(["all", "running", "attention", "success"]);
export const ATTENTION_STATUSES = "failed,degraded,warning,interrupted";
const STATUS_BY_VIEW = {
  all: "",
  running: "running",
  attention: ATTENTION_STATUSES,
  success: "success",
};
export const NEEDS_ACTION = new Set(ATTENTION_STATUSES.split(","));
const GENERIC_RUN_ERROR = "one or more core steps failed";
const JOB_TITLES = {
  init: ["初始化", "Initialize"],
  daily: ["日更", "Daily"],
  events: ["事件流", "Events"],
  backfill: ["回填", "Backfill"],
  derive: ["派生", "Derived"],
  delisted_backfill: ["退市回填", "Delisted backfill"],
  maintenance: ["维护", "Maintenance"],
};

export function runsStatusParam(view) {
  return STATUS_BY_VIEW[view] || "";
}

const ACTIONABLE_BATCH = new Set(["failed", "warning", "degraded", "interrupted", "blocked"]);

export function runsListPath(view, offset, limit) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(Math.max(0, Number(offset) || 0)));
  if (view === "attention") params.set("open", "1");
  else {
    const status = runsStatusParam(view);
    if (status) params.set("status", status);
  }
  return `/api/runs?${params.toString()}`;
}

export function openBatchFailures(batches) {
  return (batches || []).filter(
    (batch) => batch && batch.error_message && ACTIONABLE_BATCH.has(batch.status),
  );
}

export function parseRunsHash(hash) {
  const query = new URLSearchParams(String(hash || "").split("?")[1] || "");
  const requested = query.get("view") || "all";
  const raw = Number(query.get("offset") || 0);
  const preset = new URLSearchParams();
  for (const [key, value] of query) {
    if (key === "view" || key === "offset" || key === "op") continue;
    preset.append(key, value);
  }
  return {
    view: VIEWS.has(requested) ? requested : "all",
    offset: Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0,
    op: query.get("op") || "",
    preset,
  };
}

export function presetsEqual(left, right) {
  const a = left instanceof URLSearchParams ? left : new URLSearchParams(left || {});
  const b = right instanceof URLSearchParams ? right : new URLSearchParams(right || {});
  const keys = new Set([...a.keys(), ...b.keys()]);
  for (const key of keys) {
    if ((a.get(key) || "") !== (b.get(key) || "")) return false;
  }
  return true;
}

export function runsHref(changes, current) {
  const view = changes.view === undefined ? current.view : changes.view || "all";
  const offset = changes.offset === undefined ? current.offset : Math.max(0, Number(changes.offset) || 0);
  const op = changes.op === undefined ? current.op : changes.op || "";
  const source = changes.preset === undefined ? current.preset : changes.preset;
  const preset = new URLSearchParams(source || undefined);
  preset.delete("view");
  preset.delete("offset");
  preset.delete("op");
  const query = new URLSearchParams();
  if (view && view !== "all") query.set("view", view);
  if (offset > 0) query.set("offset", String(Math.floor(offset)));
  if (op) query.set("op", op);
  for (const [key, value] of preset) {
    if (value !== "") query.append(key, value);
  }
  const text = query.toString();
  return text ? `#/runs?${text}` : "#/runs";
}

export function jobTitle(name) {
  const raw = String(name || "");
  if (raw === "daily:stale" || raw.startsWith("daily:stale:")) return tr("补抓", "Catch-up");
  const family = raw.split(":")[0];
  const pair = JOB_TITLES[family];
  return pair ? tr(pair[0], pair[1]) : raw || tr("运行", "Run");
}

export function failurePreview(run, failureLine) {
  const lines = [];
  for (const failure of run.failures || []) {
    const text = failureLine(failure.error_message);
    if (text) lines.push({ dataset: failure.dataset || "", text });
  }
  const fallback = run.error_message && run.error_message !== GENERIC_RUN_ERROR ? failureLine(run.error_message) : "";
  if (!lines.length && fallback) lines.push({ dataset: "", text: fallback });
  return { shown: lines.slice(0, 2), extra: Math.max(0, lines.length - 2) };
}
