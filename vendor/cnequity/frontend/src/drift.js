// The server keeps the lake it started on. When the config file now names
// another one (or cannot be read), say so on every page rather than let the
// console and a fresh CLI quietly disagree about which lake is "the" lake.
import { tr } from "./i18n.js";

/** The two paths, each cut to what differs after their shared directory. */
export function distinctTails(a, b) {
  const left = String(a ?? "");
  const right = String(b ?? "");
  let shared = 0;
  for (let i = 0; i < Math.min(left.length, right.length) && left[i] === right[i]; i += 1) {
    if (left[i] === "/" || left[i] === "\\") shared = i + 1;
  }
  // Only worth it when the shared part is more than a root or a drive.
  if (shared <= 3) return [left, right];
  return [`…${left.slice(shared - 1)}`, `…${right.slice(shared - 1)}`];
}

/** `cne serve --config <path>`, quoted when the path needs it. */
export function restartCommand(configPath) {
  const path = String(configPath ?? "");
  // Double quotes read the same in sh, PowerShell and cmd for a plain path.
  return `cne serve --config ${/[^\w./:\\~-]/.test(path) ? `"${path}"` : path}`;
}

function brokenReason(drift, esc) {
  if (drift.kind === "missing") return tr("文件不存在。", "The file does not exist.");
  if (drift.kind === "unreadable") return tr(`无法读取：${esc(drift.detail)}。`, `Cannot read it: ${esc(drift.detail)}.`);
  const at = drift.line
    ? tr(`第 ${drift.line} 行第 ${drift.column} 列：`, `Line ${drift.line}, column ${drift.column}: `)
    : "";
  return `${at}<code>${esc(drift.detail)}</code>`;
}

export function configDriftBanner(mode, esc) {
  const drift = mode?.config_drift;
  if (!drift) return "";
  const path = (whole, shown = whole) => `<code title="${esc(whole)}">${esc(shown)}</code>`;
  if (drift.kind === "data_root") {
    const [active, configured] = distinctTails(mode.data_root, drift.configured_data_root);
    const command = restartCommand(mode.config_path);
    return `<div class="banner drift-banner" id="config-drift" role="status">
      <strong>${tr("配置文件已指向另一个数据湖，重启 serve 后生效", "The config file now points at another lake; restart serve to switch")}</strong>
      <dl class="drift-paths">
        <dt>${tr("页面数据来自", "This page shows")}</dt><dd>${path(mode.data_root, active)}</dd>
        <dt>${tr("配置文件指向", "The config file names")}</dt><dd>${path(drift.configured_data_root, configured)}</dd>
      </dl>
      <div class="drift-command"><code>${esc(command)}</code><button class="button button-ghost" type="button" data-copy-command="${esc(command)}">${tr("复制命令", "Copy command")}</button></div>
      <p class="drift-note">${tr("重启前可以照常浏览，但不能从控制台启动任务或修改设置。启动时带的其他参数请一并带上。", "Browsing works until then, but jobs and settings are locked. Keep any other options you started serve with.")}</p>
    </div>`;
  }
  return `<div class="banner drift-banner drift-broken" id="config-drift" role="alert">
    <strong>${tr("配置文件读不出来", "The config file cannot be read")}</strong>
    <p class="drift-note">${path(mode.config_path)} ${brokenReason(drift, esc)}</p>
    <p class="drift-note">${tr("页面仍在使用启动时的数据湖。修好之前，新启动的 CLI 命令会失败，控制台也不能启动任务或修改设置。", "This page still uses the lake it started with. Until the file is fixed, new CLI commands fail and the console cannot start jobs or change settings.")}</p>
  </div>`;
}

/** What a settings panel shows instead of its form while the file has drifted. */
export function driftHeldNote(home, title) {
  if (!home?.mode?.config_drift) return "";
  const text = home.mode.config_drift.kind === "data_root"
    ? tr("配置文件已指向另一个数据湖。重启 cne serve 后才能在这里修改，见页面顶部的提示。", "The config file now points at another lake. Restart cne serve to change this; see the notice at the top of the page.")
    : tr("配置文件现在读不出来。修好之后刷新页面才能在这里修改，见页面顶部的提示。", "The config file cannot be read. Fix it and refresh to change this; see the notice at the top of the page.");
  return `<div class="panel-header"><h2>${title}</h2></div><p class="panel-note">${text}</p>`;
}
