// Lake dashboard. Two views routed on the hash: the tier overview (#/) and one
// dataset (#/dataset/<name>[/state|meta]).
import { renderStorage, closeStorage } from "./storage.js";
import { renderOps, closeOps } from "./ops.js";
import { configDriftBanner } from "./drift.js";
import { closeRunStream, renderRunDetail, renderRuns } from "./runs.js";
import { disposeAll, heatmap, provenanceSeries, runGantt, severityTimeline } from "./charts.js";
import {
  ds,
  dsMarkup,
  dsSearch,
  getLang,
  grainText as partitionText,
  historyText,
  modeLabel,
  onLanguageChange,
  setLang,
  statusText,
  tierText,
  tr,
} from "./i18n.js";

const qs = new URLSearchParams(location.search);
const TOKEN = qs.get("token");
// Keep the default window compact enough to read on the overview.  The API
// supports longer windows through ?days=..., but 250 sessions makes sparse
// snapshot datasets look like a large unexplained blank block.
const DAYS = Number(qs.get("days") || 90);
const app = document.getElementById("app");

function navItems() {
  return [
    ["overview", tr("概览", "Overview"), "#/"],
    ["ops", tr("操作", "Operations"), "#/ops"],
    ["datasets", tr("数据集", "Datasets"), "#/datasets"],
    ["runs", tr("跑批", "Runs"), "#/runs"],
    ["quality", tr("质量", "Quality"), "#/quality"],
    ["storage", tr("存储运维", "Storage"), "#/storage"],
  ];
}

function pageShell(content, active = "overview") {
  const nav = navItems()
    .map(
      ([key, label, href]) =>
        `<a class="nav-link ${active === key ? "active" : ""}" href="${href}" data-nav="${key}">${label}</a>`,
    )
    .join("");
  const current = getLang();
  const zhLabel = "中文"; // i18n-ignore
  return `<div class="app-shell">
    <header class="topbar">
      <a class="brand-lockup" href="#/" aria-label="${tr("返回概览", "Back to overview")}">
        <span class="brand-mark">CNE</span>
        <span class="brand-copy"><strong>CNEquity</strong><small>research lake</small></span>
      </a>
      <span class="rail-label">Workspace</span>
      <nav class="nav" aria-label="${tr("主导航", "Main")}">${nav}</nav>
      <div class="topbar-meta"><span class="console-mode">…</span>
        <div class="lang-switch" role="group" aria-label="${tr("界面语言", "Language")}">
          <button class="lang-option${current === "zh" ? " on" : ""}" type="button" data-set-lang="zh" aria-pressed="${current === "zh"}">${zhLabel}</button>
          <button class="lang-option${current === "en" ? " on" : ""}" type="button" data-set-lang="en" aria-pressed="${current === "en"}">EN</button>
        </div>
        <button class="button button-ghost" id="refresh-page" type="button">${tr("刷新", "Refresh")}</button>
      </div>
    </header>
    <main class="page-main">${content}</main>
  </div>`;
}

function setPage(content, active = "overview") {
  app.innerHTML = pageShell(content, active);
  document.getElementById("refresh-page")?.addEventListener("click", () => location.reload());
  paintMode();
}

let modePaint = 0;
async function paintMode() {
  const ticket = ++modePaint;
  try {
    const body = await api("/api/ops");
    if (ticket !== modePaint) return;
    const el = document.querySelector(".console-mode");
    if (el) el.textContent = modeLabel(body.mode?.label || "");
    document.getElementById("config-drift")?.remove();
    const drift = configDriftBanner(body.mode, esc);
    if (drift) {
      document.querySelector(".page-main")?.insertAdjacentHTML("afterbegin", drift);
      const copy = document.querySelector("#config-drift [data-copy-command]");
      if (copy) {
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(copy.dataset.copyCommand || "");
            copy.textContent = tr("已复制", "Copied");
          } catch {
            // No clipboard permission: leave the command selected for Ctrl/Cmd+C.
            const code = document.querySelector("#config-drift .drift-command code");
            if (code) window.getSelection()?.selectAllChildren(code);
            copy.textContent = tr("已选中，按 Ctrl/⌘+C 复制", "Selected; press Ctrl/⌘+C");
          }
        };
      }
    }
    const slot = body.occupancy?.slot;
    const meta = document.querySelector(".topbar-meta");
    if (meta && slot && (slot.state === "running" || slot.state === "starting") && !meta.querySelector(".ops-live")) {
      const link = document.createElement("a");
      link.className = "ops-live";
      link.href = `#/ops/jobs/${encodeURIComponent(slot.job_id)}`;
      link.textContent = tr("任务进行中", "Job running");
      meta.prepend(link);
    }
  } catch {
    /* a page that cannot read /api/ops still renders its own error */
  }
}

function opsHref(op, params) {
  const query = new URLSearchParams();
  query.set("op", op);
  for (const [key, value] of Object.entries(params || {})) {
    if (Array.isArray(value)) query.set(key, value.join(","));
    else if (value != null && value !== "") query.set(key, String(value));
  }
  return `#/ops?${query}`;
}

async function api(path, options) {
  const sep = path.includes("?") ? "&" : "?";
  const url = TOKEN ? `${path}${sep}token=${encodeURIComponent(TOKEN)}` : path;
  // Server messages (errors, notes) come back in the page's language.
  const headers = { ...(options?.headers || {}), "X-CNE-Lang": getLang() === "en" ? "en" : "zh-CN" };
  const res = await fetch(url, { ...options, headers });
  if (!res.ok) {
    // Surface what the server said. A 422 here is usually a real contract
    // message ("requires as_of= for point-in-time queries"), and showing the
    // status code instead throws away the one useful part.
    let detail = `${path} → ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON, keep the status line */
    }
    throw new Error(detail);
  }
  return res.json();
}

const fmt = (n) => (n ?? 0).toLocaleString(getLang() === "en" ? "en" : "zh-CN");
const compact = (n) =>
  new Intl.NumberFormat(getLang() === "en" ? "en" : "zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(n ?? 0);
const mb = (b) => (!b ? "-" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${(b / 1e6).toFixed(0)} MB`);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/** First line of a batch error, without the HTTP client's docs trailer. */
function failureLine(message) {
  const line = String(message || "").split("\n")[0];
  const cut = line.indexOf(" For more information check:");
  return cut === -1 ? line : line.slice(0, cut).trimEnd();
}

// --- overview ---------------------------------------------------------------

const kpi = (n, label, note = "", tone = "") =>
  `<article class="metric-card${tone ? ` metric-card-${tone}` : ""}"><div class="metric-label">${label}</div>
  <div class="metric-value">${n}</div>${note ? `<div class="metric-note">${note}</div>` : ""}</article>`;

/** One tone map for every status word the API hands the console.
 *
 * Batch statuses, dataset freshness and audit severities were being coloured
 * by three separate inline ternaries that had already drifted: `failed` was
 * orange as a pill and red as a chip, in the same table row. They are one
 * vocabulary; keep one table. An unlisted word falls through to the neutral
 * chip rather than guessing at it. */
const TONE = {
  fresh: "fresh", success: "fresh", running: "running",
  failed: "error", error: "error", stale: "stale", warning: "stale", degraded: "stale",
  empty: "empty", info: "",
};

const statusPill = (status, label = statusText(status)) => {
  const tone = TONE[status] || "empty";
  return `<span class="status-pill status-pill-${tone}"><span class="dot ${tone === "running" ? "" : tone}"></span>${esc(label)}</span>`;
};

const chip = (label, tone = "", n) =>
  `<span class="chip${tone ? ` chip-${tone}` : ""}">${esc(label)}${n === undefined ? "" : `<b>${fmt(n)}</b>`}</span>`;

const chipRow = (items, empty = "-") =>
  items.length ? `<div class="chip-row">${items.join("")}</div>` : `<span class="muted">${empty}</span>`;

/** A status tally as chips instead of "success 12 · failed 1".
 *
 * The batch column is the densest cell on the runs table and it rendered as
 * one run-on grey string, so a failed batch read exactly like a successful one
 * until the whole cell went red. That said "something here is wrong"
 * without saying which. Per-status chips carry that on the status itself. */
const tally = (counts) => chipRow(Object.entries(counts || {}).map(([k, n]) => chip(statusText(k), TONE[k], n)));

/** A zero is a result, not an absence. */
const num = (n, cls = "") => (n ? (cls ? `<span class="${cls}">${fmt(n)}</span>` : fmt(n)) : '<span class="muted">0</span>');

/** One table, one shape.
 *
 * Every report table below was hand-rolling `<table><tr><th>` with no
 * thead/tbody and its own empty-row markup, so right-alignment and the empty
 * state drifted panel by panel. Columns are declared once: a bare string is a
 * plain header, `{h, n: true}` right-aligns the header the way the tabular
 * numbers underneath it already are. */
function dataTable(cols, rows, empty, cls = "") {
  const head = cols
    .map((c) => (typeof c === "string" ? { h: c } : c))
    .map((c) => `<th${c.n ? ' class="n"' : ""}>${esc(c.h)}</th>`)
    .join("");
  const body = rows.length
    ? rows.join("")
    : `<tr><td colspan="${cols.length}" class="empty-table"><span>${empty}</span></td></tr>`;
  return `<div class="scroll"><table${cls ? ` class="${cls}"` : ""}><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/** Load-bearing prose, not a footnote.
 *
 * The quality panels carry the two sentences most likely to be read backwards
 * `no_overlap` is "nothing comparable", not "agrees", and quarantine is
 * evidence, not a bin. They were set as small grey text under the header and
 * read as boilerplate. */
const panelNote = (html) => `<p class="panel-note">${html}</p>`;

// storage/stats.py emits exactly two of these; anything else falls through
// unchanged rather than being silently mistranslated.
function statsReason(reason) {
  if (!reason) return tr("原因未知", "Unknown reason");
  if (reason.includes("landed after the stats were built")) return tr("有新的采集批次晚于度量表", "A newer batch landed after the stats snapshot");
  if (reason.includes("no stats yet")) return tr("尚未生成过度量表", "No stats snapshot yet");
  return reason;
}

function dsLink(id) {
  return `<span class="ds-link" data-ds="${esc(id)}">${dsMarkup(id)}</span>`;
}

async function renderOverview() {
  const [h, tiers, datasets, hm] = await Promise.all([
    api("/api/health"),
    api("/api/tiers"),
    api("/api/datasets"),
    api(`/api/heatmap?days=${DAYS}`),
  ]);
  const sev = h.findings_by_severity || {};
  const notes = [];
  // The reason strings come from the ingestion layer and are English, with a
  // run UUID in them. Fine in a log, wrong in the banner of a Chinese page.
  // the id is not something the reader can act on. Translate the ones that can
  // actually appear, keep the original as a tooltip for anyone debugging.
  if (h.stats_stale) {
    notes.push({
      tone: "info",
      title: tr("度量表过期", "Stats are stale"),
      detail: tr(
        `已在后台重建（${esc(statsReason(h.stats_reason))}）。稍后刷新。`,
        `A rebuild is already running (${esc(statsReason(h.stats_reason))}). Refresh shortly.`,
      ),
    });
  }
  if (h.stale_datasets.length) {
    notes.push({
      tone: "stale",
      title: tr(`${h.stale_datasets.length} 个数据集落后`, `${h.stale_datasets.length} stale ${h.stale_datasets.length === 1 ? "dataset" : "datasets"}`),
      chips: h.stale_datasets.map((d) => dsLink(d)).join(""),
      action: { href: "#/ops?op=daily.stale", label: tr("补抓落后数据集", "Catch up stale datasets") },
    });
  }
  let primaryHref = "#/quality";
  let primaryLabel = tr("查看质量", "Open quality");
  try {
    const ops = await api("/api/ops");
    const pending = ops.occupancy?.incomplete_init;
    if (pending?.running) {
      // An empty lake during its first init is expected; offering to start
      // another init sent people into a form that could only refuse.
      notes.push({
        tone: "info",
        title: tr("初始化正在进行", "Initialization is running"),
        detail: tr("数据会随批次陆续出现，进度在运行记录里。", "Data appears batch by batch; the run shows its progress."),
        action: { href: "#/runs", label: tr("查看进度", "View progress") },
      });
      primaryHref = "#/runs";
      primaryLabel = tr("查看进度", "View progress");
    } else if (pending) {
      const resume = `#/ops?op=init.resume&run_id=${encodeURIComponent(pending.run_id)}`;
      if (ops.occupancy?.lake_empty) {
        notes.push({
          tone: "stale",
          title: tr("湖里还没有 curated 数据", "The lake has no curated data yet"),
          detail: tr("初始化会下载全市场行情主干。", "Initialization downloads the full-market price spine."),
          action: { href: "#/ops?op=init.start", label: tr("初始化数据湖", "Initialize the lake") },
        });
      }
      notes.push({
        tone: "stale",
        title: tr("初始化没跑完", "Initialization did not finish"),
        detail: tr("已成功的批次会保留。", "Batches that succeeded are kept."),
        action: { href: resume, label: tr("继续初始化", "Resume initialization") },
      });
      primaryHref = resume;
      primaryLabel = tr("继续初始化", "Resume initialization");
    } else if (ops.occupancy?.lake_empty) {
      notes.push({
        tone: "stale",
        title: tr("湖里还没有 curated 数据", "The lake has no curated data yet"),
        detail: tr("初始化会下载全市场行情主干。", "Initialization downloads the full-market price spine."),
        action: { href: "#/ops?op=init.start", label: tr("初始化数据湖", "Initialize the lake") },
      });
      primaryHref = "#/ops?op=init.start";
      primaryLabel = tr("初始化数据湖", "Initialize the lake");
    } else if (h.stale_datasets.length) {
      primaryHref = "#/ops?op=daily.stale";
      primaryLabel = tr("补抓落后数据集", "Catch up stale datasets");
    }
  } catch {
    /* overview still renders without the operations summary */
  }
  if (h.empty_required.length) {
    notes.push({
      tone: "error",
      title: tr("必需数据集为空", "Required datasets are empty"),
      chips: h.empty_required.map((d) => dsLink(d)).join(""),
    });
  }

  const byTier = {};
  for (const d of datasets) (byTier[d.tier] ||= []).push(d);

  const state = sev.error ? "error" : notes.length ? "attention" : "healthy";
  const stateLabel = {
    healthy: tr("运行正常", "Healthy"),
    attention: tr("需要关注", "Needs attention"),
    error: tr("存在错误", "Errors"),
  }[state];
  const attentionRow = (note) => {
    const body = note.chips
      ? `<div class="attention-chips">${note.chips}</div>`
      : `<p class="attention-detail">${note.detail || ""}</p>`;
    const action = note.action
      ? `<a class="button ${note.tone === "info" ? "button-ghost" : "button-primary"}" href="${note.action.href}">${esc(note.action.label)}</a>`
      : "";
    return `<li class="attention-row tone-${note.tone}"><div class="attention-copy"><strong>${esc(note.title)}</strong>${body}</div>${action}</li>`;
  };
  const attention = notes.length ? `<ul class="attention-list">${notes.map(attentionRow).join("")}</ul>` : "";
  // The status block already carries each action; the heading only repeats one
  // when no row offers it.
  const headerAction = notes.some((note) => note.action?.href === primaryHref)
    ? ""
    : `<a class="button button-primary" href="${primaryHref}">${esc(primaryLabel)}</a>`;
  const tierCards = tiers
    .map((tier) => {
      const status = tier.stale
        ? tr(`${tier.stale} 个落后`, `${tier.stale} stale`)
        : tier.empty
          ? tr(`${tier.empty} 个为空`, `${tier.empty} empty`)
          : tier.fresh === tier.datasets
            ? tr("全部最新", "All fresh")
            : tr("无落后", "Nothing stale");
      return `<details class="tier-card" ${tier.stale || tier.empty ? "open" : ""}>
        <summary><span class="tier-summary"><span class="tier-name"><span class="tier-tag">${esc(tier.tier)}</span>${esc(tierText(tier.tier, tier.label))}</span>
          <span class="tier-counts"><b>${tier.datasets}</b> ${tr("个数据集", "datasets")} · <b>${fmt(tier.rows)}</b> ${tr("行", "rows")}</span>
          <span class="tier-status ${tier.stale ? "is-stale" : tier.empty ? "is-empty" : "is-fresh"}">${status}</span></span></summary>
        <div class="tier-members">${membersTable(byTier[tier.tier] || [])}</div>
      </details>`;
    })
    .join("");

  const visibleHeatmapRows = hm.rows.filter((row) => /[#.]/.test(row.cells));
  const hiddenHeatmapRows = hm.rows.length - visibleHeatmapRows.length;
  const heatmapData = { ...hm, rows: visibleHeatmapRows };

  setPage(`
    <section class="page-heading">
      <div class="eyebrow">${tr("数据湖控制台 / 概览", "Lake console / Overview")}</div>
      <div class="heading-row"><div><h1>${tr("湖状态", "Lake status")}</h1>
        <p class="sub">${tr(`最后交易日 ${esc(h.anchor)} · ${h.datasets} 个注册数据集 · 审计快照 ${esc(h.audit_trade_date || tr("无", "none"))}`, `Last session ${esc(h.anchor)} · ${h.datasets} registered datasets · audit snapshot ${esc(h.audit_trade_date || "none")}`)}</p></div>
        <div class="action-row"><a class="button button-ghost" href="#/runs">${tr("查看跑批", "View runs")}</a>${headerAction}</div>
      </div>
    </section>
    <section class="status-hero status-${state}" aria-live="polite">
      <span class="status-icon" aria-hidden="true">${state === "healthy" ? "✓" : state === "error" ? "!" : "•"}</span>
      <div><strong>${stateLabel}</strong><span>${state === "healthy" ? tr("核心数据集已覆盖最新交易日。", "Core datasets cover the latest session.") : tr(`${notes.length} 项需要核查，数据仍可只读访问。`, `${notes.length} ${notes.length === 1 ? "item needs" : "items need"} a look. Data stays readable.`)}</span></div>
      <span class="status-anchor">${tr("基准日", "Anchor")} ${esc(h.anchor || "-")}</span>
      ${attention}
    </section>
    <section class="metric-grid" aria-label="${tr("关键指标", "Key metrics")}">
      ${kpi(num(h.datasets), tr("数据集", "Datasets"), tr("已注册", "Registered"))}
      ${kpi(num(h.fresh), tr("最新", "Fresh"), tr("最新水位", "Current watermark"))}
      ${kpi(num(h.stale, "err"), tr("落后", "Stale"), tr("超过容忍窗口", "Past the tolerance window"), h.stale ? "alert" : "")}
      ${kpi(`<span title="${fmt(h.rows)} ${tr("行", "rows")}">${compact(h.rows)}</span>`, tr("行数", "Rows"), "curated")}
      ${kpi(mb(h.bytes), tr("存储", "Storage"), "curated")}
      ${kpi(`${num(sev.error, "err")}<span class="metric-secondary"> / ${fmt(sev.warning)}</span>`, tr("审计", "Audit"), tr("错误 / 警告", "errors / warnings"), sev.error ? "alert" : "")}
    </section>
    <article class="surface-panel heat-panel"><div class="panel-header"><div><div class="eyebrow">Coverage</div><h2>${tr("覆盖热力", "Coverage")}</h2></div><span class="panel-meta">${visibleHeatmapRows.length}/${hm.rows.length} ${tr("个数据集", "datasets")} · ${hm.days.length} ${tr("个交易日", "sessions")}</span></div>
      ${visibleHeatmapRows.length ? `<div id="heat" aria-label="${tr("覆盖热力图", "Coverage heatmap")}"></div>` : `<p class="heat-empty">${tr("当前窗口内没有分区。数据较旧或还没采集时会这样，各数据集的覆盖区间见下方“数据层”。", "No partitions in the current window. That happens when the data is older or not collected yet; each dataset's range is in Layers below.")}</p>`}
      ${visibleHeatmapRows.length ? `<p class="legend"><span><i class="swatch" style="background:var(--cell-covered)"></i> ${tr("有分区覆盖", "Partition present")}</span><span><i class="swatch" style="background:var(--cell-gap)"></i> ${tr("日更源缺口", "Daily-source gap")}</span><span><i class="swatch" style="background:var(--cell-cadence)"></i> ${tr("按源节奏间隔", "Expected cadence")}</span><span><i class="swatch" style="background:var(--cell-outside)"></i> ${tr("区间外", "Outside window")}</span></p>
      <p class="heatmap-note">${tr("灰色表示当前窗口外，或该数据集按快照 / 月度 / 季度节奏采集，不等同于采集失败。", "Grey means outside this window, or the dataset is collected as a snapshot or on a monthly or quarterly cadence. It is not a failed fetch.")}${hiddenHeatmapRows ? tr(` ${hiddenHeatmapRows} 个当前没有分区的数据集已留在“数据层”中。`, ` ${hiddenHeatmapRows} datasets with no partition in this window stay in the catalog.`) : ""}</p>` : ""}
    </article>
    <section class="surface-panel dataset-panel" id="dataset-list"><div class="panel-header"><div><div class="eyebrow">Data catalog</div><h2>${tr("数据层", "Layers")}</h2></div><a class="panel-link" href="#/datasets">${tr("查看全部数据集 →", "All datasets →")}</a></div><div class="tier-grid">${tierCards}</div></section>
  `);
  if (visibleHeatmapRows.length) heatmap(document.getElementById("heat"), heatmapData);
}

function datasetOpLabel(op, why) {
  const labels = {
    "backfill.run": tr("回填这个数据集", "Backfill this dataset"),
    "daily.stale": tr("补抓落后数据", "Catch up stale data"),
    "derive.run": tr("重算派生", "Recompute derived data"),
  };
  return labels[op] || why;
}

function datasetActionButtons(dataset) {
  const runnable = (dataset.commands || []).filter((command) => command.op).slice(0, 2);
  const buttons = [`<a class="button button-ghost" href="#/datasets">${tr("← 返回数据集", "← Datasets")}</a>`];
  for (const [index, command] of runnable.entries()) {
    const tone = index === 0 ? "button button-primary" : "button button-ghost";
    buttons.push(`<a class="${tone}" href="${opsHref(command.op, command.params)}">${esc(datasetOpLabel(command.op, command.why))}</a>`);
  }
  return buttons.join("");
}

async function renderDatasets() {
  const datasets = await api("/api/datasets");
  const stale = datasets.filter((d) => d.freshness === "stale").length;
  const rows = (items) =>
    items
      .map(
        (d) => `<tr class="dataset-row"><td><span class="dot ${d.freshness === "fresh" ? "fresh" : d.freshness === "stale" ? "stale" : "empty"}"></span>${dsLink(d.dataset)}</td><td>${esc(tierText(d.tier, d.tier_label))}</td><td>${esc(historyText(d.history_mode))}</td><td>${esc(partitionText(d.granularity))}</td><td>${esc(d.watermark || "-")}</td><td class="n">${fmt(d.row_count)}</td><td class="n">${mb(d.bytes)}</td></tr>`,
      )
      .join("");
  setPage(`<section class="page-heading"><div class="eyebrow">${tr("数据湖控制台 / 数据集", "Lake console / Datasets")}</div><div class="heading-row"><div><h1>${tr("数据集", "Datasets")}</h1><p class="sub">${tr(`按注册契约浏览 ${datasets.length} 个数据集，点击名称查看状态、元数据与数据。`, `Browse ${datasets.length} registered datasets. Open a name for status, metadata, and rows.`)}</p></div>
    ${stale ? `<div class="action-row"><a class="button button-primary" href="#/ops?op=daily.stale">${tr(`补抓 ${stale} 个落后数据集`, `Catch up ${stale} stale ${stale === 1 ? "dataset" : "datasets"}`)}</a></div>` : ""}</div></section>
    <section class="surface-panel catalog-panel"><div class="catalog-toolbar"><label class="search-field"><span aria-hidden="true">⌕</span><input id="dataset-search" type="search" placeholder="${tr("搜索数据集、层级或语义", "Search name, layer, or semantics")}" autocomplete="off"></label><span class="panel-meta" id="dataset-count">${datasets.length} ${tr("个结果", "results")}</span></div><div class="scroll"><table id="dataset-table"><thead><tr><th>${tr("数据集", "Dataset")}</th><th>${tr("分层", "Layer")}</th><th>${tr("语义", "Semantics")}</th><th>${tr("粒度", "Grain")}</th><th>${tr("水位", "Watermark")}</th><th class="n">${tr("行", "Rows")}</th><th class="n">${tr("体积", "Size")}</th></tr></thead><tbody></tbody></table></div></section>`, "datasets");
  const table = document.querySelector("#dataset-table tbody");
  const count = document.getElementById("dataset-count");
  const render = (query = "") => {
    const q = query.trim().toLowerCase();
    const filtered = datasets.filter((d) =>
      [dsSearch(d.dataset), d.tier, tierText(d.tier, d.tier_label), historyText(d.history_mode), d.history_mode, partitionText(d.granularity), d.granularity].some((v) =>
        String(v || "").toLowerCase().includes(q),
      ),
    );
    table.innerHTML = filtered.length ? rows(filtered) : `<tr><td colspan="7" class="empty-table"><span>${tr("没有匹配的数据集。", "No matching dataset.")}</span></td></tr>`;
    count.textContent = `${filtered.length} ${tr("个结果", "results")}`;
  };
  document.getElementById("dataset-search").addEventListener("input", (e) => render(e.target.value));
  render();
}

function membersTable(rows) {
  const body = rows.map((d) => {
    const cls = d.freshness === "fresh" ? "fresh" : d.freshness === "stale" ? "stale" : "empty";
    const cover = d.coverage_start ? `${d.coverage_start} → ${d.coverage_end}` : "-";
    const opt = d.required ? "" : ` <span style='opacity:.6'>(${tr("可选", "optional")})</span>`;
    return `<tr class="data-row"><td><span class="dot ${cls}"></span>${dsLink(d.dataset)}${opt}</td>
      <td>${esc(historyText(d.history_mode))}</td><td>${esc(partitionText(d.granularity))}</td><td class="cell-time">${cover}</td>
      <td class="cell-time">${d.watermark || "-"}</td><td class="n">${fmt(d.row_count)}</td>
      <td class="n">${mb(d.bytes)}</td></tr>`;
  });
  return dataTable(
    [tr("数据集", "Dataset"), tr("语义", "Semantics"), tr("粒度", "Grain"), tr("覆盖", "Coverage"), tr("水位", "Watermark"), { h: tr("行", "Rows"), n: true }, { h: tr("体积", "Size"), n: true }],
    body,
    tr("这一层还没有注册数据集。", "This layer has no registered datasets."),
  );
}

// --- dataset detail ---------------------------------------------------------

function coverageBar(d) {
  if (!d.coverage_start) return `<p class="muted">${tr("尚无分区。", "No partitions yet.")}</p>`;
  const start = new Date(d.coverage_start).getTime();
  const end = new Date(d.coverage_end).getTime();
  const span = Math.max(end - start, 1);
  let horizon = "";
  if (d.earliest_available) {
    const h = new Date(d.earliest_available).getTime();
    if (h > start && h < end) {
      const pct = ((h - start) / span) * 100;
      horizon = `<div class="horizon" style="left:${pct}%" title="${tr(`源端历史天花板 ${d.earliest_available}`, `Source history ceiling ${d.earliest_available}`)}"></div>`;
    }
  }
  return `<div class="cover"><div class="fill" style="left:0;right:0"></div>${horizon}</div>
    <p class="legend"><span>${d.coverage_start}</span><span style="margin-left:auto">${d.coverage_end}</span></p>`;
}

function gapsNote(d) {
  const g = d.gaps;
  if (!g.total) return d.coverage_start ? `<p class="muted">${tr("覆盖区间内无缺口。", "No gaps inside the covered range.")}</p>` : "";
  const cadence =
    d.max_staleness_days > 1
      ? tr(
          `　该源非日更（容忍 ${d.max_staleness_days} 天），间隔属其节奏。`,
          ` This source is not daily (tolerance ${d.max_staleness_days} days); the spacing is its cadence.`,
        )
      : "";
  return `<p class="${d.max_staleness_days > 1 ? "muted" : "err"}">${tr(`${g.total} 个 ${g.unit} 无分区`, `${g.total} ${g.unit} without a partition`)}${cadence}</p>
    <p class="muted">${g.missing.slice(0, 12).map(esc).join("、")}${g.total > 12 ? " …" : ""}</p>`;
}

// Stored 15m/30m/60m resamples carry no watermark, so this is the only place
// the page can say they trail the 1m/5m they were built from.
function resampleNote(d) {
  const r = d.resample;
  if (!r) return "";
  const run = `<a href="${opsHref("derive.run", { name: d.dataset })}">${tr("重算派生", "Recompute")}</a>`;
  if (!r.derived) {
    return panelNote(r.input_sessions
      ? tr(`尚未派生。湖里的 1m / 5m 共有 ${fmt(r.input_sessions)} 个交易日可以计算，${run}后入湖。`, `Not derived yet. The lake's 1m / 5m cover ${fmt(r.input_sessions)} sessions; ${run} to store them.`)
      : tr("尚未派生。湖里还没有 1m 或 5m 分钟线。", "Not derived yet. The lake has no 1m or 5m bars."));
  }
  return panelNote(r.pending
    ? tr(`输入有 ${fmt(r.pending)} 个交易日尚未重算。1m / 5m 更新后不会自动重算，${run}后才会更新。`, `${fmt(r.pending)} input sessions have not been recomputed. Nothing reruns this when 1m / 5m change; ${run} to update.`)
    : tr("已与 1m / 5m 一致，没有待重算的交易日。", "In step with 1m / 5m; no session waits to be recomputed."));
}

function stateTab(d, prov) {
  const provTable = dataTable(
    ["source", "data_version", { h: tr("行", "Rows"), n: true }, tr("fetched_at 跨度", "fetched_at span")],
    prov.map(
      (p) => `<tr class="data-row"><td>${esc(p.source)}</td><td>${esc(p.data_version)}</td>
      <td class="n">${fmt(p.row_count)}</td>
      <td class="muted cell-time">${esc((p.fetched_at_min || "").slice(0, 10))} → ${esc((p.fetched_at_max || "").slice(0, 10))}</td></tr>`,
    ),
    tr("无溯源度量。", "No provenance stats."),
  );

  const findings = dataTable(
    ["severity", "check", "message"],
    d.findings.map(
      (f) => `<tr class="data-row"><td>${chip(f.severity, TONE[f.severity])}</td>
      <td>${esc(f.check)}</td><td>${esc(f.message)}</td></tr>`,
    ),
    tr("上次审计没有该数据集的 findings。", "The last audit has no findings for this dataset."),
  );

  const batches = dataTable(
    [tr("状态", "Status"), tr("窗口", "Window"), { h: tr("写入", "Written"), n: true }, { h: tr("重试", "Retries"), n: true }, tr("开始", "Started"), tr("错误", "Error")],
    d.batches.map(
      (b) => `<tr class="data-row"><td>${statusPill(b.status)}</td>
      <td class="muted cell-time">${esc(b.window_start || "-")} → ${esc(b.window_end || "-")}</td>
      <td class="n">${fmt(b.rows_written)}</td><td class="n">${num(b.retry_count)}</td>
      <td class="muted cell-time">${esc((b.started_at || "").slice(0, 19))}</td>
      <td class="cell-failures">${b.error_message ? esc(failureLine(b.error_message)) : '<span class="muted">-</span>'}</td></tr>`,
    ),
    tr("manifest 中没有该数据集的 batch。", "The manifest has no batch for this dataset."),
  );

  return `
    <h3>${tr("覆盖", "Coverage")}</h3>${coverageBar(d)}${resampleNote(d)}${gapsNote(d)}
    <h3>${tr("溯源分布（按时间）", "Provenance over time")}</h3><div id="prov"></div><p class="muted" id="provnote"></p>
    <h3>${tr("溯源合计", "Provenance totals")}</h3>${provTable}
    <h3>${tr("审计 findings", "Audit findings")}</h3>${findings}
    <h3>${tr("最近 batch", "Recent batches")}</h3>${batches}`;
}

const fact = (k, v) => `<div class="fact"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
const bucketText = (b) => ({ day: tr("日", "day"), month: tr("月", "month"), year: tr("年", "year") })[b] || b;
const dayCount = (n) => `${n} ${Number(n) === 1 ? "day" : "days"}`;

/** What one row covers.
 *
 * Was keyed on `intraday`, the bar-frequency field, which `trade_ticks`
 * deliberately leaves unset so it cannot inherit bar-shaped checks. That
 * printed a dash, making intraday transaction records look like a daily
 * dataset. `row_grain` is set on every intraday dataset, including the stored
 * 15m/30m/60m resamples.
 */
function rowGrainText(d) {
  if (d.row_grain === "tick") return tr("分笔（3 秒快照聚合，非 bar）", "Ticks (3-second snapshots, not bars)");
  if (d.row_grain) return `${esc(d.row_grain)} bar`;
  return tr("日", "Day");
}

/** How far back the *source* still serves, and by which mechanism.
 *
 * Two different limits reach this panel and they must not read the same. A
 * rolling per-symbol count (minute bars) moves forward every day; a fixed
 * calendar floor (trade_ticks) does not, and its horizon therefore grows.
 * Keying only on `history_horizon_days` printed "无上限" for the floor case.
 * directly contradicting the 最早可得 line right underneath it.
 */
function horizonText(d) {
  if (d.history_floor_date) return tr(`自 ${esc(d.history_floor_date)} 起（固定底，不滚动）`, `From ${esc(d.history_floor_date)} (fixed floor)`);
  if (d.history_horizon_days) return tr(`${d.history_horizon_days} 个交易日（滚动）`, `${d.history_horizon_days} sessions (rolling)`);
  return tr("无上限", "No declared limit");
}

function metaTab(d) {
  // Flags read as state, not as prose: "是" beside "否" in the same grey
  // column made the reader parse four identical rows to find the set one.
  const yn = (b) => chip(b ? tr("是", "Yes") : tr("否", "No"), b ? "fresh" : "");
  const contract = [
    fact(tr("分层", "Layer"), `${d.tier} ${esc(tierText(d.tier, d.tier_label))}`),
    fact(tr("存储层", "Storage"), d.layer),
    fact(tr("分区键", "Partition key"), d.partition_col ? `<code>${esc(d.partition_col)}</code>` : tr("单文件 merge", "Single-file merge")),
    fact(tr("分区粒度", "Partition grain"), partitionText(d.granularity)),
    fact(tr("查询日期列", "Query date column"), d.date_col ? `<code>${esc(d.date_col)}</code>` : "-"),
    fact(tr("主键", "Primary key"), d.primary_key.map((c) => `<code>${esc(c)}</code>`).join(" ")),
  ].join("");
  const semantics = [
    fact("fetch_semantics", d.fetch_semantics),
    fact("history_mode", d.history_mode),
    fact("PIT", yn(d.pit)),
    fact(tr("维护水位", "Maintains watermark"), yn(d.watermarked)),
  ].join("");
  const sources = [
    fact(tr("回填源", "Backfill source"), d.backfill_source ? esc(d.backfill_source) : "-"),
    fact(tr("源端历史视野", "Source horizon"), horizonText(d)),
    fact(tr("最早可得", "Earliest available"), d.earliest_available || tr("不受源端限制", "Not limited by the source")),
  ].join("");
  const ops = [
    fact(tr("落后容忍", "Staleness tolerance"), tr(`${d.max_staleness_days} 天`, dayCount(d.max_staleness_days))),
    fact(tr("必需", "Required"), yn(d.required)),
    fact(tr("行粒度", "Row grain"), rowGrainText(d)),
    fact(
      tr("回填分块", "Backfill chunks"),
      d.backfill_chunk_days
        ? tr(`${d.backfill_chunk_days} 天`, dayCount(d.backfill_chunk_days))
        : d.backfill_chunk_symbols
          ? tr(`${d.backfill_chunk_symbols} 标的`, `${d.backfill_chunk_symbols} symbols`)
          : `<span class="muted">${tr("不分块", "Not chunked")}</span>`,
    ),
  ].join("");

  const schema = `<div class="scroll"><table>
    <tr><th>${tr("列", "Column")}</th><th>${tr("类型", "Type")}</th><th>${tr("主键", "Key")}</th></tr>
    ${d.schema
      .map(
        (c) => `<tr><td><code>${esc(c.column)}</code></td><td class="muted">${esc(c.dtype)}</td>
      <td>${d.primary_key.includes(c.column) ? "✓" : ""}</td></tr>`,
      )
      .join("")}</table></div>`;

  const cmds = d.commands
    .map((c, i) => {
      const href = c.op ? opsHref(c.op, c.params) : "";
      return `<div class="cmd"><code id="cmd${i}">${esc(c.cmd)}</code>
      <button data-copy="cmd${i}">${tr("复制", "Copy")}</button>${href ? `<a href="${href}">${tr("在操作页运行", "Run on the operations page")}</a>` : ""}<span class="muted">${esc(c.why)}</span></div>`;
    })
    .join("");

  return `
    <h3>${tr("契约", "Contract")}</h3><div class="facts">${contract}</div>
    <h3>${tr("语义", "Semantics")}</h3><div class="facts">${semantics}</div>
    <h3>${tr("来源", "Sources")}</h3><div class="facts">${sources}</div>
    <h3>${tr("运维", "Operations")}</h3><div class="facts">${ops}</div>
    <h3>Schema</h3>${schema}
    <h3>${tr("命令", "Commands")}</h3>${cmds}
    <p class="muted">${tr("以上取自注册契约，与命令行", "Taken from the registered contract, the same as")} <code>cne contract show ${esc(d.dataset)}</code>${tr(" 一致。", ".")}</p>`;
}

// --- data tab ---------------------------------------------------------------

function kindLabel(kind) {
  return {
    trading_day: tr("交易日", "Session"),
    event_day: tr("事件日", "Event day"),
    period: tr("周期", "Period"),
    report_period: tr("报告期", "Report period"),
    none: "",
  }[kind] || "";
}

/**
 * The date control is chosen by the server's `kind`, not assumed.
 *
 * The registry spans twelve date columns in four shapes; a calendar widget over
 * `report_period` would invite a query the column cannot answer, and one over a
 * sparse event column would mostly offer days with nothing behind them. Only
 * values that exist are listed.
 */
function dataControls(d, dates) {
  const picker =
    dates.kind === "none"
      ? ""
      : `<label>${kindLabel(dates.kind)}
        <select id="q-period">${dates.values
          .map((v) => `<option value="${esc(v)}">${esc(v)}</option>`)
          .join("")}</select></label>`;
  const symbol = `<label>${tr("标的", "Symbol")} <input id="q-symbol" placeholder="600519.SH" size="12"></label>`;
  // PIT datasets have no default "current" view. load() refuses without a
  // cutoff, on purpose. Seed it with today so the tab opens on something, and
  // say what it means.
  const asOf = d.pit
    ? `<label title="${tr("PIT：只保留在该日之前已披露的事实，并取当时现行的那一版", "PIT: keep facts announced on or before this date, at the version current then")}">
         as_of <input id="q-asof" type="date" value="${new Date().toISOString().slice(0, 10)}"></label>`
    : "";
  const adjust = d.adjustable
    ? `<label>${tr("复权", "Adjust")} <select id="q-adjust">
         <option value="">${tr("不复权", "None")}</option><option value="hfq">hfq</option>
         <option value="qfq">qfq</option></select></label>`
    : "";
  const note = dates.note ? `<p class="muted">${esc(dates.note)}</p>` : "";
  return `<div class="controls">${picker}${symbol}${asOf}${adjust}
    <button id="q-run">${tr("查询", "Query")}</button></div>${note}`;
}

function rowTable(page, primaryKey) {
  if (!page.rows.length) return `<p class="muted">${tr("没有匹配的行。", "No matching rows.")}</p>`;
  const head = page.columns
    .map((c) => `<th${primaryKey.includes(c) ? ' class="pk"' : ""}>${esc(c)}</th>`)
    .join("");
  const body = page.rows
    .map((r) => `<tr>${r.map((v) => `<td>${v === null ? '<span class="muted">null</span>' : esc(v)}</td>`).join("")}</tr>`)
    .join("");
  const shown = `${page.offset + 1}-${page.offset + page.rows.length} / ${fmt(page.total)}`;
  const quality = page.adjustment;
  const factorNote = quality ? `<p class="muted">${tr("复权来源", "Factor sources")}: ${esc(Object.keys(quality.factor_sources || {}).join(", ") || "—")} · ${quality.diagnostics_assessed ? tr(`已知待核实事件 ${quality.related_event_count || 0} 个`, `${quality.related_event_count || 0} known unresolved events`) : tr("该版本的历史事件尚未核验", "Historical events for this revision have not been assessed")} · ${tr("因子覆盖不代表历史准确性；提示不影响取数。", "Factor coverage does not certify historical accuracy; diagnostics do not block reads.")}</p>` : "";
  return `${factorNote}<div class="scroll"><table class="rows"><tr>${head}</tr>${body}</table></div>
    <div class="controls">
      <button id="q-prev" ${page.offset === 0 ? "disabled" : ""}>${tr("上一页", "Previous")}</button>
      <button id="q-next" ${page.offset + page.limit >= page.total ? "disabled" : ""}>${tr("下一页", "Next")}</button>
      <span class="muted">${shown}</span>
    </div>`;
}

async function dataTab(d, host) {
  const enc = encodeURIComponent(d.dataset);
  const dates = await api(`/api/datasets/${enc}/dates`);
  host.innerHTML = `${dataControls(d, dates)}<div id="q-out"></div>`;

  const state = { offset: 0 };
  const out = document.getElementById("q-out");

  async function run() {
    const params = new URLSearchParams();
    const period = document.getElementById("q-period")?.value;
    const symbol = document.getElementById("q-symbol")?.value.trim();
    const asOf = document.getElementById("q-asof")?.value;
    const adjust = document.getElementById("q-adjust")?.value;
    if (period) params.set("period", period);
    if (symbol) params.set("symbol", symbol);
    if (asOf) params.set("as_of", asOf);
    if (adjust) params.set("adjust", adjust);
    params.set("offset", String(state.offset));
    out.innerHTML = `<p class="muted">${tr("查询中…", "Querying…")}</p>`;
    try {
      const page = await api(`/api/datasets/${enc}/rows?${params}`);
      out.innerHTML = rowTable(page, d.primary_key);
      const prev = document.getElementById("q-prev");
      const next = document.getElementById("q-next");
      if (prev) prev.onclick = () => { state.offset = Math.max(0, state.offset - page.limit); run(); };
      if (next) next.onclick = () => { state.offset += page.limit; run(); };
    } catch (err) {
      out.innerHTML = `<p class="err">${esc(err.message)}</p>`;
    }
  }

  document.getElementById("q-run").onclick = () => {
    state.offset = 0;
    run();
  };
  run();
}

async function renderDetail(name, tab) {
  setPage(`<div class="loading-state"><span>${tr(`加载 ${esc(ds(name))}…`, `Loading ${esc(ds(name))}…`)}</span></div>`, "datasets");
  const enc = encodeURIComponent(name);
  const [d, series, prov] = await Promise.all([
    api(`/api/datasets/${enc}`),
    api(`/api/datasets/${enc}/provenance/series`),
    api(`/api/datasets/${enc}/provenance`),
  ]);
  const cls = d.freshness === "fresh" ? "fresh" : d.freshness === "stale" ? "stale" : "empty";
  setPage(`
    <section class="page-heading">
      <div class="eyebrow">${tr("数据湖控制台 / 数据集", "Lake console / Datasets")} / ${esc(d.tier)}</div>
      <div class="heading-row"><div><div class="page-title-with-status"><h1>${dsMarkup(d.dataset)}</h1>${statusPill(cls)}</div>
        <p class="sub">${esc(tierText(d.tier, d.tier_label))} · ${esc(d.layer)} · ${esc(historyText(d.history_mode))}${d.required ? "" : tr(" · 可选数据集", " · optional")}</p></div>
        <div class="action-row">${datasetActionButtons(d)}</div>
      </div>
    </section>
    <section class="metric-grid detail-metrics" aria-label="${tr("数据集关键指标", "Dataset metrics")}">
      ${kpi(`<span title="${fmt(d.row_count)} ${tr("行", "rows")}">${compact(d.row_count)}</span>`, tr("行数", "Rows"), "curated")}
      ${kpi(mb(d.bytes), tr("存储", "Storage"), "curated")}
      ${kpi(esc(d.watermark || "-"), tr("水位", "Watermark"), d.watermarked ? tr("维护中", "Maintained") : tr("不维护水位", "No watermark"))}
      ${kpi(esc(d.coverage_end || "-"), tr("覆盖至", "Covered through"), partitionText(d.granularity))}
    </section>
    <section class="surface-panel detail-workspace">
    <nav class="tabs" aria-label="${tr("数据集详情标签页", "Dataset tabs")}">
      ${["state", "meta", "data"]
        .map(
          (name) =>
            `<a class="tab ${tab === name ? "on" : ""}" href="#/dataset/${enc}/${name}" aria-current="${tab === name ? "page" : "false"}">${
              { state: tr("状态", "Status"), meta: tr("元数据", "Metadata"), data: tr("数据", "Data") }[name]
            }</a>`,
        )
        .join("")}
    </nav>
    <div id="tabbody" class="tab-body">${tab === "meta" ? metaTab(d) : tab === "data" ? "" : stateTab(d, prov)}</div>
    </section>`, "datasets");

  if (tab === "state") {
    provenanceSeries(document.getElementById("prov"), series);
    document.getElementById("provnote").textContent = tr(`每点跨度：${bucketText(series.bucket)}`, `Each point spans a ${bucketText(series.bucket)}`);
  } else if (tab === "data") {
    await dataTab(d, document.getElementById("tabbody"));
  }

  app.querySelectorAll("button[data-copy]").forEach((b) => {
    b.onclick = () => navigator.clipboard?.writeText(document.getElementById(b.dataset.copy).textContent);
  });
}


// --- quality ----------------------------------------------------------------

async function renderQuality() {
  const q = await api("/api/quality");
  const latestFindings = q.findings_runs[0]?.by_severity || {};
  const quarantineFiles = q.quarantine.reduce((sum, item) => sum + (item.files || 0), 0);
  const quarantineBytes = q.quarantine.reduce((sum, item) => sum + (item.bytes || 0), 0);
  const cacheEntries = q.on_demand.reduce((sum, item) => sum + (item.entries || 0), 0);

  // check×n joined by "、" was a wall of grey the eye slid off. Same counts,
  // one chip each, so the frequent check is findable without reading the row.
  const checks = (pairs) => chipRow(pairs.map(([c, n]) => chip(c, "", n)));

  const findingsRows = q.findings_runs.map((r) => {
    const sev = r.by_severity;
    return `<tr class="data-row">
      <td class="cell-time"><a class="table-link" href="#/quality/${encodeURIComponent(r.run_id)}">${esc(r.trade_date || "-")}</a></td>
      <td class="n">${num(sev.error, "err")}</td>
      <td class="n">${num(sev.warning)}</td>
      <td class="n">${num(sev.info)}</td>
      <td>${checks(r.top_checks)}</td>
    </tr>`;
  });

  const diffRows = q.diff_runs.map(
    (r) => `<tr class="data-row">
      <td class="cell-time"><a class="table-link" href="#/quality/${encodeURIComponent(r.run_id)}">${esc(r.trade_date || "-")}</a></td>
      <td class="n">${num(r.diff_count)}</td>
      <td>${checks(Object.entries(r.by_check))}</td>
    </tr>`,
  );

  const quarantineRows = q.quarantine.map(
    (e) => `<tr class="data-row"><td><code>${esc(e.name)}</code></td><td class="n">${fmt(e.files)}</td>
      <td class="n">${mb(e.bytes)}</td><td class="muted cell-time">${esc(e.modified.slice(0, 10))}</td></tr>`,
  );

  const onDemandRows = q.on_demand.map(
    (e) => `<tr class="data-row"><td>${dsLink(e.dataset)}</td><td class="n">${fmt(e.entries)}</td>
      <td class="n">${mb(e.bytes)}</td><td class="muted cell-time">${esc((e.newest || "").slice(0, 10)) || "-"}</td></tr>`,
  );

  setPage(`
    <section class="page-heading"><div class="eyebrow">${tr("数据湖控制台 / 质量", "Lake console / Quality")}</div><div class="heading-row"><div><h1>${tr("质量", "Quality")}</h1><p class="sub">${tr("审计、跨源比对、隔离区与按需缓存的只读证据面板。", "Read-only evidence for audits, cross-source diffs, quarantine, and on-demand cache.")}</p></div>
      <div class="action-row"><a class="${latestFindings.error ? "button button-primary" : "button button-ghost"}" href="#/ops?op=check.audit">${tr("全湖审计", "Full-lake audit")}</a></div></div></section>
    <section class="metric-grid quality-metrics" aria-label="${tr("质量关键指标", "Quality metrics")}">
      ${kpi(num(q.findings_runs.length), tr("审计快照", "Audit snapshots"), q.findings_runs[0]?.trade_date || tr("暂无", "None yet"))}
      ${kpi(num(latestFindings.error, "err"), tr("最新错误", "Latest errors"), tr("最近一次审计", "latest audit"), latestFindings.error ? "alert" : "")}
      ${kpi(num(latestFindings.warning), tr("最新警告", "Latest warnings"), tr("最近一次审计", "latest audit"))}
      ${kpi(num(quarantineFiles), tr("隔离文件", "Quarantine files"), mb(quarantineBytes))}
      ${kpi(num(cacheEntries), tr("按需缓存", "On-demand cache"), tr("条目", "entries"))}
    </section>
    <div class="report-stack">
    <section class="surface-panel report-panel"><div class="panel-header"><div><div class="eyebrow">Audit findings</div><h2>${tr("审计趋势", "Audit trend")}</h2></div><span class="panel-meta">${tr("按审计日", "By audit date")}</span></div>
    <div id="sev-chart"></div>
    ${dataTable(
      [tr("审计日", "Audit date"), { h: "error", n: true }, { h: "warning", n: true }, { h: "info", n: true }, tr("主要 check", "Top checks")],
      findingsRows,
      tr("还没有 findings。", "No findings yet."),
    )}</section>

    <section class="surface-panel report-panel"><div class="panel-header"><div><div class="eyebrow">Cross-source</div><h2>${tr("跨源比对", "Cross-source")}</h2></div><span class="panel-meta">${tr("主源 vs 备源", "Primary vs backup")}</span></div>
    ${panelNote(tr(
      `主源与备源在同一天同一字段上的分歧。<strong><code>no_overlap</code> 是「两边没有共同主键可比」，不是「一致」</strong>。这是这张表最容易被读反的一行。`,
      `Disagreement between the primary and backup source on the same day and field. <strong><code>no_overlap</code> means there is no shared key to compare, not that they agree</strong>.`,
    ))}
    ${dataTable(
      [tr("审计日", "Audit date"), { h: tr("差异", "Diffs"), n: true }, tr("按 check", "By check")],
      diffRows,
      tr("还没有跨源比对产物。", "No cross-source output yet."),
    )}</section>

    <section class="surface-panel report-panel"><div class="panel-header"><div><div class="eyebrow">Quarantine</div><h2>${tr("隔离区", "Quarantine")}</h2></div><span class="panel-meta">${tr("保留问题证据", "Kept as evidence")}</span></div>
    ${panelNote(tr(
      `<strong>不是垃圾桶。</strong>这些是因为有问题而被撤出 curated 的数据，留着当证据。删之前先看清楚是什么。`,
      `<strong>Not a trash bin.</strong> These rows were pulled out of curated because something was wrong, and they are kept as evidence. Read them before deleting.`,
    ))}
    ${dataTable(
      [tr("目录", "Directory"), { h: tr("文件", "Files"), n: true }, { h: tr("体积", "Size"), n: true }, tr("最后修改", "Modified")],
      quarantineRows,
      tr("隔离区是空的。", "Quarantine is empty."),
    )}</section>

    <section class="surface-panel report-panel"><div class="panel-header"><div><div class="eyebrow">On-demand</div><h2>${tr("按需缓存", "On-demand cache")}</h2></div><span class="panel-meta">${tr("不进入 curated", "Not curated")}</span></div>
    ${panelNote(tr(
      `按 symbol 抓取、缓存在 <code>meta/on_demand/</code>，不进 curated。面板别处看不到它们。`,
      `Fetched per symbol and cached under <code>meta/on_demand/</code>. They are not curated and do not appear elsewhere.`,
    ))}
    ${dataTable(
      [tr("数据集", "Dataset"), { h: tr("条目", "Entries"), n: true }, { h: tr("体积", "Size"), n: true }, tr("最新", "Newest")],
      onDemandRows,
      tr(
        `还没有人查过 on-demand 数据集（<code>stock_news</code> / <code>research_reports</code>）。这是正常状态，不是缺口。`,
        `Nobody has queried the on-demand datasets (<code>stock_news</code> / <code>research_reports</code>) yet. That is expected, not a gap.`,
      ),
    )}</section></div>`, "quality");

  severityTimeline(document.getElementById("sev-chart"), q.findings_runs);
}

async function renderQualityRun(runId) {
  const d = await api(`/api/quality/runs/${encodeURIComponent(runId)}`);
  // severity was coloured only when it read exactly "error", so a warning row
  // looked identical to an info row. It is the column the reader sorts by eye.
  const table = (rows, cols) =>
    dataTable(
      cols,
      rows.map(
        (r) => `<tr class="data-row">${cols
          .map((c) => {
            const v = r[c];
            if (v === undefined || v === null || v === "") return '<td><span class="muted">-</span></td>';
            if (c === "severity") return `<td>${chip(statusText(v), TONE[v])}</td>`;
            if (c === "dataset") return `<td>${dsLink(v)}</td>`;
            return `<td>${esc(v)}</td>`;
          })
          .join("")}</tr>`,
      ),
      tr("无。", "None."),
    );

  setPage(`
    <section class="page-heading"><div class="eyebrow">${tr("数据湖控制台 / 质量 / 审计详情", "Lake console / Quality / Audit")}</div><div class="heading-row"><div><h1>${esc(d.trade_date || runId.slice(0, 8))}</h1><p class="sub"><code>${esc(d.run_id)}</code></p></div><div class="action-row"><a class="button button-ghost" href="#/quality">${tr("← 返回质量", "← Quality")}</a></div></div></section>
    <section class="metric-grid metrics-2" aria-label="${tr("审计关键指标", "Audit metrics")}">${kpi(num(d.findings.length), "Findings", tr("审计发现", "Audit findings"))}${kpi(num(d.diffs.length), "Diffs", tr("跨源差异", "Cross-source diffs"))}</section>
    <div class="report-stack"><section class="surface-panel report-panel"><div class="panel-header"><div><div class="eyebrow">Findings</div><h2>${tr("审计发现", "Findings")}</h2></div><span class="panel-meta">${d.findings.length}</span></div>${table(d.findings, ["severity", "dataset", "check", "message"])}</section>
    <section class="surface-panel report-panel"><div class="panel-header"><div><div class="eyebrow">Cross-source</div><h2>${tr("跨源差异", "Cross-source diffs")}</h2></div><span class="panel-meta">${d.diffs.length}</span></div>${table(d.diffs, ["severity", "dataset", "check", "field", "bps", "message"])}</section></div>`, "quality");
}

function runCtx() {
  return {
    api,
    setPage,
    esc,
    dataTable,
    kpi,
    statusPill,
    tally,
    dsLink,
    fmt,
    num,
    compact,
    failureLine,
    token: TOKEN,
  };
}

// --- routing ----------------------------------------------------------------

async function route() {
  disposeAll();
  closeRunStream();
  closeStorage();
  closeOps();
  const dataset = location.hash.match(/^#\/dataset\/([^/]+)(?:\/(state|meta|data))?/);
  const run = location.hash.match(/^#\/runs\/([^/?]+)/);
  const qrun = location.hash.match(/^#\/quality\/(.+)$/);
  const opsJob = location.hash.match(/^#\/ops\/jobs\/([^/?]+)/);
  const opsPage = location.hash === "#/ops" || location.hash.startsWith("#/ops?");
  try {
    if (opsJob) await renderOps({ api, setPage, esc, dataTable, jobId: decodeURIComponent(opsJob[1]) });
    else if (opsPage) await renderOps({ api, setPage, esc, dataTable });
    else if (location.hash === "#/storage") await renderStorage({ api, setPage, esc, dataTable });
    else if (dataset) await renderDetail(decodeURIComponent(dataset[1]), dataset[2] || "state");
    else if (run) await renderRunDetail(decodeURIComponent(run[1]), runCtx());
    else if (location.hash.startsWith("#/runs")) await renderRuns(runCtx());
    else if (qrun) await renderQualityRun(decodeURIComponent(qrun[1]));
    else if (location.hash.startsWith("#/quality")) await renderQuality();
    else if (location.hash.startsWith("#/datasets")) await renderDatasets();
    else await renderOverview();
    if (!location.hash.includes("?")) window.scrollTo(0, 0);
  } catch (err) {
    if (String(err.message || "").includes("尚未配置")) { // i18n-ignore
      await renderOps({ api, setPage, esc, dataTable });
      return;
    }
    setPage(`<section class="error-state"><div class="eyebrow">CNEquity</div><h1>${tr("加载失败", "Failed to load")}</h1><p class="sub err">${esc(err.message)}</p><a class="button button-primary" href="#/">${tr("返回概览", "Back to overview")}</a></section>`);
  }
}

// Delegated so it survives every re-render.
document.addEventListener("click", (e) => {
  const langButton = e.target.closest("[data-set-lang]");
  if (langButton) {
    setLang(langButton.dataset.setLang);
    return;
  }
  const link = e.target.closest("[data-ds]");
  if (link) {
    e.stopPropagation();
    location.hash = `#/dataset/${encodeURIComponent(link.dataset.ds)}`;
  }
});
onLanguageChange(() => route());
window.addEventListener("hashchange", route);
route();
