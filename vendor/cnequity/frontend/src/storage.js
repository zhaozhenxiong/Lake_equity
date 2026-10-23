// A review is a proposal. Only a separate, explicit confirmation executes it.
import { dsMarkup, dsSearch, dsWithCode, locale, tr } from "./i18n.js";

const canPick = o => !o.current && o.reasons.every(r => r === "recent_generation");
const reasonText = code => ({
  current_generation: tr("当前版本", "Current version"),
  recent_generation: tr("最近 5 代", "Latest 5 generations"),
  explicit_hold: tr("审计或人工保留", "Audit or manual hold"),
  referenced_path: tr("仍有路径引用", "Still referenced"),
  missing_current_pointer: tr("缺少当前版本指针", "Missing current pointer"),
  unreceipted_generation: tr("缺少版本收据", "Missing generation receipt"),
  missing_file_manifest: tr("缺少文件清单", "Missing file manifest"),
  incomplete_batches: tr("仍有未完成批次", "Incomplete batches remain"),
  not_compacted: tr("尚未成功合并", "Not successfully compacted"),
  running: tr("运行中", "Running"),
  active_run_lock: tr("运行锁被占用", "Run lock is held"),
  lifecycle_hold: tr("已登记保留", "Registered hold"),
  orphan_candidate: tr("孤立暂存，需另行核实", "Orphan staging needs separate review"),
  young_orphan: tr("新近孤立暂存", "Recent orphan staging"),
}[code] || code);
const statusText = code => ({
  due: tr("已过观察期", "Past the observation period"),
  observing: tr("观察期内", "Still observing"),
  unmarked: tr("尚未标记", "Not marked"),
  protected: tr("受保护", "Protected"),
}[code] || code);
const size = n => n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(2)} GiB` : n >= 2 ** 20 ? `${(n / 2 ** 20).toFixed(2)} MiB` : `${(n / 1024).toFixed(1)} KiB`;
const countText = items => `${items.length.toLocaleString(locale())} ${tr("项", "items")} · ${size(items.reduce((n, o) => n + o.logical_bytes, 0))}`;
const bytesOf = items => items.reduce((n, o) => n + o.logical_bytes, 0);
const datasetId = o => {
  if (o.kind !== "revisions") return "";
  const mark = " · 第 "; // i18n-ignore
  const cut = o.label.indexOf(mark);
  return cut > 0 ? o.label.slice(0, cut) : o.label;
};
const groupKey = o => (o.kind !== "revisions" ? o.kind : datasetId(o));
const groupTitle = key => (key === "staging" ? tr("采集暂存", "Ingestion staging") : dsWithCode(key));
const versionName = o => {
  if (o.kind !== "revisions") return o.label;
  const mark = " · 第 "; // i18n-ignore
  const cut = o.label.indexOf(mark);
  if (cut < 0) return o.label;
  const revision = o.label.slice(cut + mark.length).replace(/版$/, "").trim(); // i18n-ignore
  return tr(`第 ${revision} 版`, `rev ${revision}`);
};
const why = o => {
  if (o.current) return tr("正在读取", "In use");
  if (canPick(o)) {
    const bits = [];
    if (o.status === "due" || o.status === "observing" || o.status === "unmarked") bits.push(statusText(o.status));
    if (o.reasons.includes("recent_generation")) bits.push(reasonText("recent_generation"));
    return bits.join(" · ") || tr("可以删除", "Deletable");
  }
  return o.reasons.map(reasonText).join(tr("、", ", ")) || statusText("protected");
};
const datasetCell = item => (item.kind !== "revisions" ? escHtml(groupTitle(item.kind)) : dsMarkup(datasetId(item)));
const escHtml = value => String(value ?? "").replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
let generation = 0;
export function closeStorage() { generation++; }

export async function renderStorage({ api, setPage, esc, dataTable }) {
  const current = ++generation;
  let summary, review, running = false;
  let view = "deletable";
  let query = "";
  let picked = new Set();
  let listed = [];
  let groupMembers = new Map();
  const openGroups = new Set();
  const alive = () => current === generation;
  const post = (path, body) => api(path, {
    method: "POST", headers: { "Content-Type": "application/json", "X-CNE-Storage-CSRF": summary.csrf_token },
    body: JSON.stringify(body),
  });
  const rows = (objects, kind) => objects.map(o => {
    const item = kind ? { ...o, kind } : o;
    return `<tr><td>${datasetCell(item)}</td><td>${esc(versionName(item))}</td><td class="n">${size(o.logical_bytes)}</td></tr>`;
  });
  function showMessage(text, error = false) {
    const target = document.getElementById("storage-message");
    if (target && alive()) { target.className = `panel-note${error ? " err" : ""}`; target.textContent = text; }
  }
  function controls(disabled) {
    running = disabled;
    document.querySelectorAll("[data-storage-action], #storage-query").forEach(el => { el.disabled = disabled; });
    if (!disabled) refreshSelection();
  }
  async function poll(job) {
    while (alive() && ["checking", "executing"].includes(job.status)) {
      await new Promise(resolve => setTimeout(resolve, 800));
      if (!alive()) return null;
      job = await api(`/api/storage/jobs/${job.job_id}`);
    }
    if (!alive()) return null;
    if (job.status === "error") throw new Error(`${job.error} ${job.message || ""}`);
    return job;
  }
  function showReview(job) {
    review = job;
    const purge = job.phase === "purge";
    const target = document.getElementById("storage-review");
    if (!job.objects.length) {
      target.innerHTML = "";
      showMessage(purge
        ? tr("本次检查没有可删除的项目。未到期、被引用或受保护的数据会继续保留。", "This check found nothing to delete. Items not yet due, still referenced, or protected are kept.")
        : tr("没有可标记的项目。", "Nothing to mark."));
      return;
    }
    const count = job.objects.length;
    showMessage(tr("检查完成。请核对下方清单；确认在 10 分钟内有效，执行时会再次核验保护条件。", "Check complete. Review the list below. The confirmation is valid for 10 minutes, and protections are verified again when it runs."));
    target.innerHTML = `<section class="surface-panel report-panel storage-confirm" aria-labelledby="storage-review-title">
      <div class="panel-header"><div><div class="eyebrow">${purge ? tr("等待删除确认", "Awaiting deletion confirmation") : tr("开始观察期", "Start observation period")}</div><h2 id="storage-review-title" tabindex="-1">${purge ? tr(`确认永久删除 ${count} 项`, `Confirm permanent deletion of ${count} items`) : tr(`确认标记 ${count} 项`, `Confirm marking ${count} items`)}</h2></div><span class="panel-meta">${tr(`账面大小 ${size(job.logical_bytes)}`, `Logical size ${size(job.logical_bytes)}`)}</span></div>
      <p class="panel-note">${purge ? tr("只处理以下清单。删除后无法从原路径读取这些历史数据；当前版本及受保护对象继续保留。APFS 克隆的账面大小不等于实际释放空间。", "Only the list below is processed. Once deleted, this history can no longer be read from its original path; the current version and protected items are kept. For APFS clones, logical size is not the space actually freed.") : tr("只记录待删除状态，至少观察 7 天，不释放空间；未变化的已有标记保留原观察起点。到期后仍须再次检查并在网页确认删除。", "This only records the pending-deletion state for an observation period of at least 7 days and frees no space; unchanged existing marks keep their original start. Once due, run the check again and confirm deletion on this page.")}${job.resuming ? tr(" 此清单包含上次操作已完成的项目，恢复执行会跳过它们。", " This list includes items the previous run already finished; resuming skips them.") : ""}</p>
      <div class="storage-confirm-scroll">${dataTable([tr("数据集", "Dataset"), tr("版本", "Version"), { h: tr("账面大小", "Logical size"), n: true }], rows(job.objects, job.kind), tr("没有项目", "No items"))}</div>
      <div class="storage-consent"><label><input type="checkbox" id="storage-ack">${purge ? tr("我已核对清单，理解删除不可撤销，确认删除这些历史数据。", "I checked the list, understand this cannot be undone, and confirm deleting this history.") : tr("我已核对清单，确认开始观察期。", "I checked the list and confirm starting the observation period.")}</label>
      ${purge ? `<label><input type="checkbox" id="storage-idle">${tr("我已停止外部查询、其他 serve 实例和采集调度，确保维护期间不会重新启动。", "I stopped external queries, other serve instances and collection schedules, and they will not restart during maintenance.")}</label>` : ""}
      <div class="action-row"><button class="button ${purge ? "button-danger" : "button-primary"}" id="storage-confirm" type="button" disabled>${purge ? tr(`永久删除这 ${count} 项`, `Permanently delete these ${count}`) : tr(`标记这 ${count} 项`, `Mark these ${count}`)}</button><button class="button button-ghost" id="storage-cancel" type="button">${tr("取消", "Cancel")}</button></div></div></section>`;
    const button = document.getElementById("storage-confirm");
    const consent = () => { button.disabled = !document.getElementById("storage-ack").checked || (purge && !document.getElementById("storage-idle").checked); };
    target.querySelectorAll("input").forEach(el => el.addEventListener("change", consent));
    document.getElementById("storage-cancel").onclick = () => { review = null; target.innerHTML = ""; showMessage(tr("已取消，没有执行此清单。", "Cancelled. This list was not run.")); };
    button.onclick = async () => {
      if (running || !review) return;
      controls(true);
      target.querySelectorAll("button, input").forEach(el => { el.disabled = true; });
      showMessage(purge
        ? tr("正在执行已确认的清单。本面板读取暂时暂停，请保持外部任务停止。", "Running the confirmed list. This panel pauses its reads; keep external jobs stopped.")
        : tr("正在记录观察期…", "Recording the observation period…"));
      try {
        const job = await post("/api/storage/confirm", { review_id: review.job_id, confirmation_token: review.confirmation_token, confirmed: true, maintenance_confirmed: purge });
        await finish(job);
      } catch (err) { if (alive()) { target.innerHTML = ""; review = null; await reloadAfterError(err); } }
      finally { if (alive()) controls(false); }
    };
    document.getElementById("storage-review-title").focus();
  }
  function showDeletion(parts) {
    const target = document.getElementById("storage-review");
    if (!target || !parts.length) return;
    const objects = parts.flatMap(part => part.objects || []);
    const logical = parts.reduce((sum, part) => sum + Number(part.journal.logical_bytes_deleted || 0), 0);
    const before = parts.find(part => typeof part.journal.filesystem_free_bytes_before === "number")?.journal.filesystem_free_bytes_before;
    const after = [...parts].reverse().find(part => typeof part.journal.filesystem_free_bytes_after === "number")?.journal.filesystem_free_bytes_after;
    const disk = typeof before === "number" && typeof after === "number"
      ? (after - before > 0
        ? tr(`磁盘可用空间增加 ${size(after - before)}。删除前 ${size(before)}，删除后 ${size(after)}。`, `Free disk space grew by ${size(after - before)}: ${size(before)} before deletion, ${size(after)} after.`)
        : tr(`磁盘可用空间没有增加（删除前 ${size(before)}，删除后 ${size(after)}）。账面大小不能当成实际腾出的空间。`, `Free disk space did not grow (${size(before)} before deletion, ${size(after)} after). Logical size is not space actually freed.`))
      : tr("没有读到磁盘可用空间的变化。账面大小不能当成实际腾出的空间。", "Could not read the change in free disk space. Logical size is not space actually freed.");
    const lines = parts.flatMap(part => (part.objects || []).map(o => ({ ...o, kind: o.kind || part.kind })));
    target.innerHTML = `<section class="surface-panel report-panel storage-result" aria-labelledby="storage-deleted-title"><div class="panel-header"><div><div class="eyebrow">${tr("删除结果", "Deletion result")}</div><h2 id="storage-deleted-title" tabindex="-1">${tr(`已删除 ${objects.length} 项`, `Deleted ${objects.length} items`)}</h2></div><span class="panel-meta">${tr(`账面 ${size(logical)}`, `Logical ${size(logical)}`)}</span></div><p class="panel-note">${disk}</p><div class="storage-confirm-scroll">${dataTable([tr("数据集", "Dataset"), tr("版本", "Version"), { h: tr("账面大小", "Logical size"), n: true }], rows(lines), tr("没有可显示的版本名称", "No version names to show"))}</div></section>`;
    showMessage(tr(`已删除 ${objects.length} 项，账面 ${size(logical)}。`, `Deleted ${objects.length} items, ${size(logical)} logical size.`));
    document.getElementById("storage-deleted-title")?.focus();
  }
  async function finish(job) {
    const known = review ? review.objects : [];
    const result = await poll(job);
    if (!result) return;
    const purge = result.phase === "purge";
    summary = await api("/api/storage");
    if (!alive()) return;
    paint();
    if (purge) showDeletion([{ objects: known, journal: result.result || {}, kind: review?.kind }]);
    else showMessage(tr("已标记并开始观察期。至少 7 天后重新检查，删除仍需在此页面确认。", "Marked; the observation period has started. Check again in 7 days or more; deletion still needs confirmation on this page."));
  }
  async function reloadAfterError(err) {
    try { summary = await api("/api/storage"); if (alive()) paint(); } catch { /* retain the original error */ }
    showMessage(err.message, true);
  }
  async function check(phase, resume) {
    if (running) return;
    controls(true);
    review = null;
    document.getElementById("storage-review").innerHTML = "";
    showMessage(tr("正在核验引用、版本和文件完整性。大目录检查可能需要几分钟；这一步不会删除数据。", "Verifying references, versions, and file integrity. Large directories can take a few minutes; this step deletes nothing."));
    try {
      const kind = resume?.kind || "revisions";
      const job = await poll(await post("/api/storage/reviews", { kind, phase, ...(resume ? { resume_plan_id: resume.plan_id } : {}) }));
      if (job) showReview(job);
    } catch (err) { showMessage(err.message, true); }
    finally { if (alive()) controls(false); }
  }
  function chosenItems() {
    return summary.objects.filter(o => picked.has(o.object_id) && canPick(o));
  }
  function askDelete() {
    const chosen = chosenItems();
    if (running || !chosen.length) return;
    const target = document.getElementById("storage-review");
    target.innerHTML = `<section class="surface-panel report-panel storage-confirm" aria-labelledby="storage-review-title">
      <div class="panel-header"><div><div class="eyebrow">${tr("删除确认", "Confirm deletion")}</div><h2 id="storage-review-title" tabindex="-1">${tr(`删除 ${chosen.length.toLocaleString(locale())} 项历史数据`, `Delete ${chosen.length.toLocaleString(locale())} history items`)}</h2></div><span class="panel-meta">${tr("账面", "Logical size")} ${size(bytesOf(chosen))}</span></div>
      <p class="panel-note">${tr("下面是将要删除的全部项目。正在使用和受保护的版本不在其中。点「永久删除」后会先核验，核验结果和这份清单一致才删除。一次超过 200 项会分批进行。", "Everything below would be deleted. In-use and protected versions are not included. Permanently delete checks the list first and continues only when the check matches. More than 200 items are checked in batches.")}</p>
      <div class="storage-confirm-scroll">${dataTable([tr("数据集", "Dataset"), tr("版本", "Version"), { h: tr("账面大小", "Logical size"), n: true }], rows(chosen), tr("没有项目", "No items"))}</div>
      <div class="storage-consent"><label><input type="checkbox" id="storage-ack">${tr("我已核对清单，理解删除不可撤销。", "I checked the list and understand this cannot be undone.")}</label>
      <label><input type="checkbox" id="storage-idle">${tr("外部查询、其他 serve 和采集调度已经停止，维护期间不会重新启动。", "External queries, other serve processes and collection schedules are stopped and will stay stopped.")}</label>
      <div class="action-row"><button class="button button-danger" id="storage-confirm" type="button" disabled>${tr(`永久删除这 ${chosen.length.toLocaleString(locale())} 项`, `Permanently delete these ${chosen.length.toLocaleString(locale())}`)}</button><button class="button button-ghost" id="storage-cancel" type="button">${tr("取消", "Cancel")}</button></div></div></section>`;
    const button = document.getElementById("storage-confirm");
    const consent = () => { button.disabled = !document.getElementById("storage-ack").checked || !document.getElementById("storage-idle").checked; };
    target.querySelectorAll("input").forEach(el => el.addEventListener("change", consent));
    document.getElementById("storage-cancel").onclick = () => { target.innerHTML = ""; showMessage(tr("已取消，没有删除。", "Cancelled. Nothing was deleted.")); };
    button.onclick = () => deleteSelected(chosen);
    target.scrollIntoView({ block: "start" });
    document.getElementById("storage-review-title").focus();
  }
  async function deleteSelected(chosen) {
    if (running || !chosen.length) return;
    controls(true);
    review = null;
    document.getElementById("storage-review").innerHTML = "";
    const done = [];
    try {
      for (const kind of ["revisions", "staging"]) {
        const group = chosen.filter(o => o.kind === kind);
        for (let index = 0; index < group.length; index += 200) {
          const slice = group.slice(index, index + 200);
          const range = `${index + 1}–${index + slice.length} / ${group.length}`;
          showMessage(kind === "revisions"
            ? tr(`正在核验勾选的历史版本（${range}）。核验通过后会删除这些项目。`, `Verifying the selected old revisions (${range}). They are deleted once the check passes.`)
            : tr(`正在核验已合并暂存（${range}）。核验通过后会删除这些项目。`, `Verifying compacted staging (${range}). It is deleted once the check passes.`));
          const job = await poll(await post("/api/storage/reviews", { kind, phase: "purge", object_ids: slice.map(o => o.object_id) }));
          if (!job) return;
          const same = job.objects.map(o => o.object_id).sort().join("\n") === slice.map(o => o.object_id).sort().join("\n");
          if (!same) {
            showReview(job);
            showMessage(tr("核验后的清单和勾选不一致，没有继续删除。请核对下方清单后再确认。", "The verified list differs from the selection, so nothing more was deleted. Review the list below, then confirm."));
            return;
          }
          showMessage(tr("正在删除已勾选的项目。本面板读取暂时暂停，请保持外部任务停止。", "Deleting the selected items. This panel pauses its reads; keep external jobs stopped."));
          const result = await poll(await post("/api/storage/confirm", { review_id: job.job_id, confirmation_token: job.confirmation_token, confirmed: true, maintenance_confirmed: true }));
          if (!result) return;
          done.push({ objects: job.objects, journal: result.result || {}, kind });
        }
      }
      summary = await api("/api/storage");
      if (!alive()) return;
      paint();
      showDeletion(done);
    } catch (err) {
      if (!alive()) return;
      try { summary = await api("/api/storage"); if (alive()) paint(); } catch { /* retain the original error */ }
      if (done.length && alive()) showDeletion(done);
      showMessage(`${done.length ? tr("部分项目已经删除。", "Some items were already deleted. ") : ""}${err.message}`, true);
    } finally { if (alive()) controls(false); }
  }
  function refreshSelection() {
    const button = document.getElementById("storage-delete-selected");
    const meta = document.getElementById("storage-selection-meta");
    const all = document.getElementById("storage-select-listed");
    if (!button || !meta || running) return;
    const chosen = chosenItems();
    const bytes = bytesOf(chosen);
    const ids = listed.map(o => o.object_id);
    const allOn = ids.length > 0 && ids.every(id => picked.has(id));
    meta.textContent = chosen.length
      ? tr(
          `已选 ${chosen.length.toLocaleString(locale())} 项 · 账面 ${size(bytes)}。点删除后先给确认单，不会立刻删除。`,
          `${chosen.length.toLocaleString(locale())} selected · ${size(bytes)} logical size. Delete opens a confirmation and does not remove anything yet.`,
        )
      : tr("还没有勾选。点数据集前的框会选中该数据集里列出的全部历史版本。", "Nothing selected. Checking a dataset selects every listed version in it.");
    button.textContent = chosen.length
      ? tr(`删除所选 ${chosen.length.toLocaleString(locale())} 项`, `Delete ${chosen.length.toLocaleString(locale())} selected`)
      : tr("删除所选", "Delete selected");
    button.disabled = !chosen.length;
    if (all) {
      all.textContent = allOn
        ? tr("取消全选", "Clear selection")
        : tr(`全选列出的 ${ids.length.toLocaleString(locale())} 项`, `Select all ${ids.length.toLocaleString(locale())} listed`);
      all.disabled = !ids.length;
    }
    document.querySelectorAll(".storage-pick").forEach(el => { el.checked = picked.has(el.dataset.objectId); });
    document.querySelectorAll("[data-group]").forEach(el => {
      const members = groupMembers.get(el.dataset.group) || [];
      const on = members.length > 0 && members.every(id => picked.has(id));
      el.checked = on;
      el.indeterminate = !on && members.some(id => picked.has(id));
    });
  }
  function renderList() {
    const list = document.getElementById("storage-list");
    const dock = document.getElementById("storage-dock");
    if (!list || !dock) return;
    const q = query.trim().toLowerCase();
    const match = o => !q || `${dsSearch(datasetId(o))} ${groupTitle(groupKey(o))} ${o.label} ${why(o)}`.toLowerCase().includes(q);
    const buckets = {
      deletable: summary.objects.filter(o => canPick(o) && match(o)),
      current: summary.objects.filter(o => o.current && match(o)),
      kept: summary.objects.filter(o => !o.current && !canPick(o) && match(o)),
    };
    listed = buckets.deletable;
    if (view === "current") {
      const items = [...buckets.current].sort((a, b) => groupTitle(groupKey(a)).localeCompare(groupTitle(groupKey(b)), locale()));
      list.innerHTML = `<p class="panel-note">${tr("数据湖正在读取这些版本，清理不会选中它们。", "The lake is reading these versions. Cleanup will not select them.")}</p>${dataTable([tr("正在读取", "In use"), { h: tr("账面大小", "Logical size"), n: true }], items.map(o => `<tr><td>${datasetCell(o)} ${esc(versionName(o))}</td><td class="n">${size(o.logical_bytes)}</td></tr>`), q ? tr("没有符合筛选的当前版本。", "No current version matches the filter.") : tr("还没有已提交的当前版本。", "No committed current version yet."))}`;
      dock.innerHTML = "";
      return;
    }
    const picking = view === "deletable";
    const items = picking ? buckets.deletable : buckets.kept;
    const groups = new Map();
    for (const item of items) {
      const key = groupKey(item);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    const ordered = [...groups.entries()].sort((a, b) => bytesOf(b[1]) - bytesOf(a[1]) || groupTitle(a[0]).localeCompare(groupTitle(b[0]), locale()));
    groupMembers = new Map(ordered.map(([name, members]) => [name, members.map(o => o.object_id)]));
    const note = picking
      ? tr("按账面大小从大到小。勾选数据集会带上其中列出的每个历史版本，包括折叠着的。最近 5 代和观察期内的历史也可以删。", "Largest logical size first. Checking a dataset selects every listed version in it, including collapsed ones. The latest 5 generations and versions still in observation can be deleted.")
      : tr("这些历史不能删除。原因写在每一行上：当前指针以外的引用、人工保留、缺少收据或归档，或暂存仍有未完成批次。", "These items cannot be deleted. Each row says why: references, holds, missing receipts or archives, or unfinished staging batches.");
    const body = ordered.map(([name, members]) => {
      const versions = [...members].sort((a, b) => versionName(b).localeCompare(versionName(a), "zh", { numeric: true }));
      const opened = Boolean(q) || openGroups.has(name);
      const table = dataTable(
        picking ? [tr("选择", "Select"), tr("版本", "Version"), tr("说明", "Note"), { h: tr("账面大小", "Logical size"), n: true }] : [tr("版本", "Version"), tr("不能删除的原因", "Why it stays"), { h: tr("账面大小", "Logical size"), n: true }],
        versions.map(o => picking
          ? `<tr><td><input type="checkbox" class="storage-pick" data-storage-action data-object-id="${esc(o.object_id)}" aria-label="${tr(`选择 ${groupTitle(groupKey(o))} ${versionName(o)}`, `Select ${groupTitle(groupKey(o))} ${versionName(o)}`)}"></td><td title="${esc(o.object_id)}">${esc(versionName(o))}</td><td>${esc(why(o))}</td><td class="n">${size(o.logical_bytes)}</td></tr>`
          : `<tr><td title="${esc(o.object_id)}">${esc(versionName(o))}</td><td>${esc(why(o))}</td><td class="n">${size(o.logical_bytes)}</td></tr>`),
        tr("没有版本。", "No versions."),
      );
      const box = picking
        ? `<input type="checkbox" data-storage-action data-group="${esc(name)}" aria-label="${tr(`选择 ${groupTitle(name)} 的全部可删除版本`, `Select every deletable version of ${groupTitle(name)}`)}">`
        : "";
      return `<section class="storage-group"><div class="storage-group-head">${box}<button type="button" class="storage-group-toggle" data-toggle="${esc(name)}" aria-expanded="${opened ? "true" : "false"}">${esc(groupTitle(name))}</button><span class="storage-group-meta">${countText(versions)}</span></div><div class="storage-group-body"${opened ? "" : " hidden"}>${table}</div></section>`;
    }).join("");
    list.innerHTML = `<p class="panel-note">${note}</p>${body || `<p class="empty-table">${q ? tr("没有符合筛选的项目。", "Nothing matches the filter.") : picking ? tr("没有可以删除的历史版本。", "No deletable history.") : tr("没有受保护的历史版本。", "No protected history.")}</p>`}`;
    dock.innerHTML = picking
      ? `<p id="storage-selection-meta">${tr("还没有勾选。", "Nothing selected.")}</p><div class="action-row"><button class="button button-ghost" type="button" data-storage-action id="storage-select-listed">${tr("全选列出的可删除项", "Select listed items")}</button><button class="button button-danger" type="button" data-storage-action id="storage-delete-selected" disabled>${tr("删除所选", "Delete selected")}</button></div>`
      : "";
    list.querySelectorAll(".storage-pick").forEach(el => el.addEventListener("change", () => {
      if (el.checked) picked.add(el.dataset.objectId); else picked.delete(el.dataset.objectId);
      refreshSelection();
    }));
    list.querySelectorAll("[data-group]").forEach(el => el.addEventListener("change", () => {
      for (const id of groupMembers.get(el.dataset.group) || []) {
        if (el.checked) picked.add(id); else picked.delete(id);
      }
      refreshSelection();
    }));
    list.querySelectorAll("[data-toggle]").forEach(el => el.addEventListener("click", () => {
      const bodyNode = el.parentElement.nextElementSibling;
      const open = bodyNode.hidden;
      bodyNode.hidden = !open;
      el.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) openGroups.add(el.dataset.toggle); else openGroups.delete(el.dataset.toggle);
    }));
    const selectListed = document.getElementById("storage-select-listed");
    if (selectListed) selectListed.onclick = () => {
      const ids = listed.map(o => o.object_id);
      const allOn = ids.every(id => picked.has(id));
      for (const id of ids) { if (allOn) picked.delete(id); else picked.add(id); }
      refreshSelection();
    };
    const remove = document.getElementById("storage-delete-selected");
    if (remove) remove.onclick = askDelete;
    if (picking) refreshSelection();
  }
  function setView(next) {
    if (next === view) return;
    view = next;
    document.querySelectorAll("[data-view]").forEach(el => {
      const on = el.dataset.view === view;
      el.classList.toggle("on", on);
      el.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderList();
  }
  function paint() {
    review = null;
    picked = new Set();
    const toolsOpen = document.getElementById("storage-tools")?.open ?? false;
    const live = summary.objects.filter(o => o.current);
    const deletable = summary.objects.filter(canPick);
    const kept = summary.objects.filter(o => !o.current && !canPick(o));
    const metric = (id, title, items, note) => `<button type="button" class="storage-metric${view === id ? " on" : ""}" data-storage-action data-view="${id}" aria-pressed="${view === id}"><span class="metric-label">${title}</span><span class="metric-value">${items.length.toLocaleString("zh-CN")}</span><span class="metric-note">${note || countText(items)}</span></button>`;
    const unfinished = summary.unfinished.length ? `<section class="surface-panel report-panel storage-review"><h2>${tr("有未完成的删除记录", "An unfinished deletion is still open")}</h2><p class="panel-note">${tr("先重新审核原清单。已完成的项目不会重复删除，剩余项目仍需确认。", "Review the original list again. Finished items are not deleted twice; the rest still need confirmation.")}</p>${summary.unfinished.map((x, i) => `<div class="storage-resume"><code>${esc(x.plan_id)}</code><button class="button button-primary" data-storage-action data-resume="${i}" type="button">${tr("重新审核", "Review again")} ${x.kind === "revisions" ? tr("版本", "versions") : tr("暂存", "staging")}</button></div>`).join("")}</section>` : "";
    setPage(`<section class="page-heading"><div class="eyebrow">${tr("数据湖控制台 / 存储运维", "Lake console / Storage")}</div><div class="heading-row"><div><h1>${tr("存储运维", "Storage")}</h1><p class="sub">${tr("先看可以删除的历史。勾选后点删除，确认单核对通过才会执行。正在使用和受保护的版本分开列出，不能被选中。", "Start with history that can be deleted. Delete opens a confirmation and runs only after that check passes. In-use and protected versions are listed separately and cannot be selected.")}</p></div><span class="status-pill status-pill-fresh">${tr("自动删除已关闭", "Automatic deletion is off")}</span></div></section>
      ${unfinished}
      ${!summary.registered ? `<p class="panel-note err">${tr("尚未导入生命周期引用清单，按到期规则的检查会因缺少保护依据而停止。先在命令行运行 <code>cne storage import</code> 完成登记。", "The lifecycle reference list has not been imported, so a retention-rule check stops for lack of protection evidence. Run <code>cne storage import</code> first.")}</p>` : ""}
      <div class="storage-metrics">${metric("deletable", tr("可以删除", "Deletable"), deletable, tr(`${size(bytesOf(deletable))} 账面`, `${size(bytesOf(deletable))} logical`))}${metric("current", tr("正在使用", "In use"), live, tr("不会进入删除", "Not deletable"))}${metric("kept", tr("受保护", "Protected"), kept, tr("不能勾选", "Cannot select"))}<div class="storage-metric is-static"><span class="metric-label">${tr("磁盘可用", "Disk free")}</span><span class="metric-value">${size(summary.disk_free_bytes)}</span><span class="metric-note">${tr("共", "of")} ${size(summary.disk_total_bytes)}</span></div></div>
      <p id="storage-message" class="panel-note" role="status" aria-live="polite">${tr("勾选历史版本后点删除。浏览或刷新此页不会删除数据。大小都是账面值，不能当成实际腾出的空间。", "Select history, then delete. Opening or refreshing this page does not delete anything. Sizes are logical values, not space you will get back.")}</p>
      <div id="storage-review" class="storage-review"></div>
      <section class="surface-panel storage-board"><div class="storage-toolbar"><label for="storage-query">${tr("筛选", "Filter")}</label><input id="storage-query" type="search" value="${esc(query)}" placeholder="${tr("数据集、版本或原因", "Dataset, version, or reason")}"></div><div id="storage-dock"></div><div id="storage-list"></div></section>
      <details class="storage-fold" id="storage-tools"${toolsOpen ? " open" : ""}><summary>${tr("按到期规则检查", "Check by retention rule")}</summary><div class="storage-fold-body"><p class="panel-note">${tr("这一步只找出已经过观察期、或还没开始观察的项目，不会按上面的勾选删除。手动删除不需要先做检查。", "This only finds items past observation, or not yet observing. It does not delete the selection above. A manual delete does not need this check.")}</p><div class="storage-actions"><button class="button button-primary" data-storage-action id="storage-check" type="button">${tr("检查到期项目", "Check due items")}</button><button class="button button-ghost" data-storage-action id="storage-mark" type="button">${tr("检查待标记项目", "Check unmarked items")}</button></div></div></details>`, "storage");
    document.getElementById("storage-query").oninput = event => { query = event.target.value; renderList(); };
    document.querySelectorAll("[data-view]").forEach(el => { el.onclick = () => setView(el.dataset.view); });
    document.getElementById("storage-check").onclick = () => check("purge");
    document.getElementById("storage-mark").onclick = () => check("mark");
    document.querySelectorAll("[data-resume]").forEach(el => { el.onclick = () => check("purge", summary.unfinished[Number(el.dataset.resume)]); });
    renderList();
  }
  setPage(`<section class="page-heading"><h1>${tr("存储运维", "Storage")}</h1><p class="sub" role="status">${tr("正在读取存储状态…", "Reading storage status…")}</p></section>`, "storage");
  summary = await api("/api/storage");
  if (!alive()) return;
  paint();
  const active = summary.active_jobs[0];
  if (active) {
    controls(true);
    showMessage(active.status === "executing"
      ? tr("已有用户确认的操作正在执行，正在读取进度…", "A confirmed operation is already running. Reading its progress…")
      : tr("正在等待已有检查完成…", "Waiting for the running check to finish…"));
    try { if (active.status === "executing") await finish(active); else { const job = await poll(active); if (job) showReview(job); } }
    catch (err) { await reloadAfterError(err); }
    finally { if (alive()) controls(false); }
  }
}
