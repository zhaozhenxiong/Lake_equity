// Lake switches. A preview writes nothing; apply updates the config file only.
import { driftHeldNote } from "./drift.js";
import { pick, tr } from "./i18n.js";
export async function renderSettings(ctx, home, alive, flash = "") {
  const { api, esc } = ctx;
  const host = document.getElementById("ops-settings");
  if (!host) return;
  const revision = (host.settingsRevision || 0) + 1;
  host.settingsRevision = revision;
  const pageAlive = alive;
  alive = () => pageAlive() && host.settingsRevision === revision;
  const held = driftHeldNote(home, tr("取数设置", "Fetch settings"));
  if (held) {
    host.innerHTML = held;
    return;
  }
  let value;
  try {
    value = await api("/api/ops/settings");
  } catch (err) {
    if (alive()) host.innerHTML = `<p class="panel-note err">${tr("取数设置", "Fetch settings")}: ${esc(err.message)}</p>`;
    return;
  }
  if (!alive()) return;
  const writable = home.mode.ops_enabled;
  const disabled = writable ? "" : "disabled";
  const sections = (value.sections || [])
    .map((section) => {
      const fields = (section.settings || []).map((spec) => field(spec, disabled, esc)).join("");
      return `<h3>${esc(pick(section, "title"))}</h3>${fields}`;
    })
    .join("");
  host.innerHTML = `<div class="panel-header"><h2>${tr("取数设置", "Fetch settings")}</h2></div>
    <p class="panel-note">${esc(pick(value, "note") || "")}</p>
    ${value.push2_env_note ? `<p class="panel-note">${esc(pick(value, "push2_env_note"))}</p>` : ""}
    ${writable ? "" : `<p class="panel-note">${tr("当前模式不能修改这些设置。", "This mode cannot change these settings.")}</p>`}
    ${flash ? `<p class="panel-note" id="settings-saved">${esc(flash)}</p>` : `<p class="panel-note" id="settings-saved" hidden></p>`}
    <form id="settings-fields" class="ops-fields">${sections}
      <div class="action-row"><button class="button button-primary" type="submit" ${disabled}>${tr("预览取数设置", "Preview fetch settings")}</button></div>
    </form><div id="settings-preview"></div>`;
  const form = document.getElementById("settings-fields");
  const target = document.getElementById("settings-preview");
  const specs = (value.sections || []).flatMap((section) => section.settings || []);
  let previewGeneration = 0;
  form.oninput = () => {
    previewGeneration += 1;
    target.innerHTML = "";
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!writable) return;
    const current = ++previewGeneration;
    target.innerHTML = `<p class="panel-note">${tr("正在预览…", "Previewing…")}</p>`;
    const values = {};
    for (const spec of specs) {
      const input = document.getElementById(`setting-${spec.id}`);
      values[spec.id] = spec.kind === "bool" ? Boolean(input.checked) : input.value;
    }
    try {
      const preview = await api("/api/ops/settings/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CNE-CSRF": home.csrf_token },
        body: JSON.stringify({ values }),
      });
      if (!alive() || current !== previewGeneration) return;
      if (!preview.token) {
        target.innerHTML = `<p class="panel-note">${esc(pick(preview, "note") || tr("没有需要保存的改动。", "Nothing to save."))}</p>`;
        return;
      }
      const items = (preview.changes || [])
        .map((change) => `<li>${esc(pick(change, "label"))}${tr("：", ": ")}${esc(pick(change, "before_label"))} → ${esc(pick(change, "after_label"))}</li>`)
        .join("");
      target.innerHTML = `<p class="panel-note">${esc(pick(preview, "note") || "")}</p>
        ${preview.push2_env_note ? `<p class="panel-note">${esc(preview.push2_env_note)}</p>` : ""}
        <ul class="ops-list">${items}</ul>
        <label class="ops-check"><input id="settings-ack" type="checkbox"> ${esc(pick(preview, "acknowledgement"))}</label>
        <div class="action-row"><button class="button button-primary" id="settings-apply" type="button" disabled>${tr("保存设置", "Save settings")}</button></div>
        <p class="panel-note" id="settings-note"></p>`;
      const button = document.getElementById("settings-apply");
      button.disabled = true;
      document.getElementById("settings-ack").onchange = (change) => {
        button.disabled = !change.target.checked;
      };
      button.onclick = async () => {
        if (!alive() || current !== previewGeneration) return;
        button.disabled = true;
        try {
          const saved = await api("/api/ops/settings/apply", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CNE-CSRF": home.csrf_token },
            body: JSON.stringify({ token: preview.token, acknowledged: true }),
          });
          if (alive()) {
            await renderSettings(
              ctx,
              home,
              pageAlive,
              tr(
                `已保存。原文件备份为 ${saved.backup_name}。这次没有启动取数。`,
                `Saved. The original file was backed up as ${saved.backup_name}. No fetching was started.`,
              ),
            );
          }
        } catch (err) {
          if (alive()) document.getElementById("settings-note").textContent = tr(`${err.message} 请重新预览。`, `${err.message} Preview again.`);
        }
      };
    } catch (err) {
      if (alive() && current === previewGeneration) target.innerHTML = `<p class="panel-note err">${esc(err.message)}</p>`;
    }
  };
}

function field(spec, disabled, esc) {
  const scope = spec.scope ? `<small class="muted">${esc(pick(spec, "scope"))}</small>` : "";
  const help = spec.help ? `<small class="muted">${esc(pick(spec, "help"))}</small>` : "";
  if (spec.kind === "bool") {
    return `<label class="ops-check"><input id="setting-${esc(spec.id)}" type="checkbox" ${spec.value ? "checked" : ""} ${disabled}> ${esc(pick(spec, "label"))}</label>${help}${scope}`;
  }
  const options = (spec.choices || [])
    .map((choice) => `<option value="${esc(choice.value)}" ${choice.value === spec.value ? "selected" : ""}>${esc(pick(choice, "label"))}</option>`)
    .join("");
  return `<label>${esc(pick(spec, "label"))}<select id="setting-${esc(spec.id)}" ${disabled}>${options}</select></label>${help}${scope}`;
}
