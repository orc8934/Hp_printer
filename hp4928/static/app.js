/* HP 4928 打印服务 — 前端交互（原生 JS，无构建步骤） */
(() => {
  "use strict";

  // ------------------------------------------------------------ 基础工具
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";

  const escapeHtml = (text) =>
    String(text ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtBytes = (bytes) => {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  };

  const fmtTime = (iso) => {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const pad = (v) => String(v).padStart(2, "0");
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    const hhmm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return sameDay ? hhmm : `${date.getMonth() + 1}/${date.getDate()} ${hhmm}`;
  };

  function toast(message, kind = "", timeout = 4200) {
    const host = $("#toast-host");
    if (!host) return;
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    host.appendChild(node);
    setTimeout(() => node.remove(), timeout);
  }

  async function api(path, options = {}) {
    const opts = Object.assign({ headers: {} }, options);
    opts.headers["Accept"] = "application/json";
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    if (opts.method && opts.method !== "GET") opts.headers["X-CSRF-Token"] = CSRF;
    const response = await fetch(path, opts);
    if (response.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      throw new Error("未登录");
    }
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = { ok: false, error: `服务器返回异常（HTTP ${response.status}）` };
    }
    if (!response.ok || data.ok === false) {
      const error = new Error(data.error || `请求失败（HTTP ${response.status}）`);
      error.payload = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function flatten(source, prefix = "", out = {}) {
    Object.entries(source || {}).forEach(([key, value]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (value && typeof value === "object" && !Array.isArray(value)) flatten(value, path, out);
      else out[path] = value;
    });
    return out;
  }

  // ------------------------------------------------------------ 全局状态
  let bootstrap = {};
  try {
    bootstrap = JSON.parse($("#bootstrap-json")?.textContent || "{}");
  } catch (err) {
    bootstrap = {};
  }

  const state = {
    schema: bootstrap.schema || null,
    status: bootstrap.status || {},
    jobs: bootstrap.jobs || [],
    limits: bootstrap.limits || { max_upload_mb: 64 },
    urls: bootstrap.urls || [],
    mode: "document",
    values: {},
    native: {},
    files: [],
    submitting: false,
    pollTimer: null,
    modalJob: null,
    seenFlip: new Set(),
  };
  if (state.schema) {
    state.values = flatten(state.schema.defaults || {});
  }

  const currentPage = document.body.dataset.page;

  // ------------------------------------------------------------ 状态条
  function renderStatus(status) {
    state.status = status || {};
    const dot = $("#status-dot");
    const text = $("#status-text");
    const driver = $("#status-driver");
    const urls = $("#status-urls");
    if (!dot || !text) return;

    const backend = state.status.backend || {};
    dot.className = "status-dot";
    if (backend.kind === "cups" && !backend.degraded) {
      dot.classList.add("ok");
      text.innerHTML = `真机打印 · CUPS 队列 <b>${escapeHtml(backend.queue || "")}</b>`;
    } else if (backend.kind === "cups") {
      dot.classList.add("warn");
      text.innerHTML = `CUPS 队列 <b>${escapeHtml(backend.queue || "")}</b> 可用，但有提醒`;
    } else {
      dot.classList.add("warn");
      text.innerHTML = `虚拟打印机（未连实物打印机）· ${escapeHtml(backend.reason || "")}`;
    }

    const usb = state.status.usb || {};
    if (driver) {
      const driverInfo = state.status.driver || {};
      driver.innerHTML = `
        <span class="muted tiny">USB：${usb.present ? "已连接" : "未检测到"}</span>
        <span class="muted tiny">· 驱动：${escapeHtml(driverInfo.source || "—")}</span>`;
    }
    if (urls) {
      urls.textContent = (state.urls || []).slice(0, 2).join("  ");
    }
  }

  // ------------------------------------------------------------ 设置面板
  const PRESETS = [
    { id: "bw-a4", label: "黑白 A4 单面（默认）", mode: "document",
      values: { media: "A4", color: "mono", duplex: "off", quality: "normal", pages_per_sheet: 1 } },
    { id: "bw-duplex", label: "黑白双面（手动）", mode: "document",
      values: { media: "A4", color: "mono", duplex: "long-edge", quality: "normal", "reverse_second_pass": true } },
    { id: "color", label: "彩色文档", mode: "document",
      values: { color: "color", quality: "best", media_type: "plain" } },
    { id: "draft", label: "省墨草稿", mode: "document",
      values: { color: "mono", quality: "draft" } },
    { id: "photo4x6", label: "6 寸照片 4R", mode: "photo",
      values: { "photo.media": "4x6in", media: "4x6in", "photo.fit": "fit",
                "photo.border_mm": 3, "photo.dpi": 300, color: "color", quality: "photo",
                media_type: "photo" } },
    { id: "two-up", label: "每张 2 页", mode: "document",
      values: { pages_per_sheet: 2, duplex: "off" } },
  ];

  function getValue(key) {
    return state.values[key];
  }

  function setValue(key, value) {
    state.values[key] = value;
  }

  function fieldVisible(field) {
    const cond = field.visible_if;
    if (!cond) return true;
    return Object.entries(cond).every(([key, allowed]) => {
      const current = String(getValue(key) ?? "");
      return Array.isArray(allowed) ? allowed.map(String).includes(current) : String(allowed) === current;
    });
  }

  function optionLabel(field, option) {
    let label = escapeHtml(option.label || option.value);
    if (field.key === "media" && option.support) {
      const map = { official: ["官方", "official"], custom: ["自定义", "custom"], unsupported: ["不支持", "unsupported"] };
      const entry = map[option.support];
      if (entry) label += ` <span class="media-support ${entry[1]}">${entry[0]}</span>`;
    }
    return label;
  }

  function renderField(field) {
    if (!fieldVisible(field)) return "";
    const key = field.key;
    const value = getValue(key);
    const drive = field.drive ? `<span class="field-drive">→ ${escapeHtml(field.drive)}</span>` : "";
    const hint = field.hint ? `<span class="field-hint">${escapeHtml(field.hint)}</span>` : "";

    if (field.type === "bool") {
      const checked = value === true || value === "true" || value === 1 ? "checked" : "";
      return `<label class="switch" title="${escapeHtml(field.hint || "")}">
        <input type="checkbox" data-key="${escapeHtml(key)}" data-type="bool" ${checked}
          ${field.disabled ? "disabled" : ""}>
        <span>${escapeHtml(field.label)}${drive}</span></label>`;
    }

    if (field.type === "select") {
      const options = (field.options || []).map((option) => {
        const raw = option.value ?? option.name ?? "";
        const selected = String(raw) === String(value) ? "selected" : "";
        return `<option value="${escapeHtml(raw)}" ${selected}>${optionLabel(field, option)}</option>`;
      }).join("");
      return `<label class="field">
        <span>${escapeHtml(field.label)} ${drive}</span>
        <select data-key="${escapeHtml(key)}" data-type="select" ${field.disabled ? "disabled" : ""}>
          ${options}
        </select>${hint}</label>`;
    }

    if (field.type === "int" || field.type === "number") {
      const step = field.step ?? (field.type === "int" ? 1 : 0.5);
      return `<label class="field">
        <span>${escapeHtml(field.label)} ${drive}</span>
        <input type="number" data-key="${escapeHtml(key)}" data-type="number"
          value="${escapeHtml(value ?? "")}" min="${field.min ?? ""}" max="${field.max ?? ""}"
          step="${step}" ${field.disabled ? "disabled" : ""}>${hint}</label>`;
    }

    return `<label class="field">
      <span>${escapeHtml(field.label)} ${drive}</span>
      <input type="text" data-key="${escapeHtml(key)}" data-type="text"
        value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(field.placeholder || "")}"
        ${field.disabled ? "disabled" : ""}>${hint}</label>`;
  }

  function renderSettings() {
    const host = $("#settings-panel");
    if (!host || !state.schema) return;
    const groups = (state.schema.groups || []).filter((group) => {
      if (group.mode === "photo") return state.mode === "photo";
      return true;
    });

    host.innerHTML = groups.map((group, index) => {
      const fields = (group.fields || []).map(renderField).join("");
      // 只默认折叠「高级」，照片/双面等常用项保持展开
      const collapsed = group.id === "advanced" && index > 1 ? " collapsed" : "";
      return `<div class="group${collapsed}" data-group="${escapeHtml(group.id)}">
        <div class="group-head" data-toggle-group>
          <span class="chev">▼</span><span>${escapeHtml(group.label)}</span>
        </div>
        <div class="group-body">${fields}</div>
      </div>`;
    }).join("");

    $$("[data-toggle-group]", host).forEach((head) => {
      head.addEventListener("click", () => head.closest(".group").classList.toggle("collapsed"));
    });

    $$("[data-key]", host).forEach((input) => {
      input.addEventListener("input", onFieldChange);
      input.addEventListener("change", onFieldChange);
    });

    renderNotes();
  }

  function onFieldChange(event) {
    const input = event.currentTarget;
    const key = input.dataset.key;
    let value;
    if (input.dataset.type === "bool") value = input.checked;
    else if (input.dataset.type === "number") value = input.value === "" ? "" : Number(input.value);
    else value = input.value;

    setValue(key, value);

    // 双面 vs 每张多页：互斥提示
    if (key === "duplex" && String(value) !== "off") {
      if (Number(getValue("pages_per_sheet") || 1) > 1) {
        toast("手动双面与「每张多页」不能同时使用，已把每张页数改回 1", "warn");
        setValue("pages_per_sheet", 1);
        renderSettings();
        return;
      }
    }
    if (key === "pages_per_sheet" && Number(value) > 1 && String(getValue("duplex")) !== "off") {
      setValue("duplex", "off");
      toast("已自动改为单面（手动双面不支持每张多页）", "warn");
      renderSettings();
      return;
    }

    // 照片模式：纸张尺寸跟随相纸
    if (state.mode === "photo" && key === "photo.media") {
      setValue("media", value);
    }

    // 只重绘受 visible_if 影响的部分，避免输入框失焦
    const needsRerender = $$("[data-key]", $("#settings-panel")).some((node) => {
      const group = state.schema.groups.find((item) =>
        (item.fields || []).some((field) => field.key === node.dataset.key));
      const field = group?.fields.find((item) => item.key === node.dataset.key);
      return field && field.visible_if && Object.keys(field.visible_if).includes(key.replace(/^.*\./, ""));
    });
    if (needsRerender) renderSettings();
    renderNotes();
  }

  function renderNotes() {
    const host = $("#settings-notes");
    if (!host || !state.schema) return;
    const notes = [...(state.schema.notes || [])];
    if (state.mode === "photo") {
      notes.unshift("照片模式：默认按 6 寸相纸（4R 102×152mm）排版，保留 3mm 安全边距。");
    }
    host.innerHTML = notes.slice(0, 4).map((note) => `· ${escapeHtml(note)}`).join("<br>");
  }

  function renderPresets() {
    const host = $("#presets");
    if (!host) return;
    host.innerHTML = PRESETS.map((preset) =>
      `<button class="preset-btn" type="button" data-preset="${preset.id}">${escapeHtml(preset.label)}</button>`
    ).join("");
    $$("[data-preset]", host).forEach((button) => {
      button.addEventListener("click", () => {
        const preset = PRESETS.find((item) => item.id === button.dataset.preset);
        if (!preset) return;
        switchMode(preset.mode, false);
        Object.entries(preset.values).forEach(([key, value]) => setValue(key, value));
        renderSettings();
        toast(`已套用：${preset.label}`, "ok", 1800);
      });
    });
  }

  function renderNative() {
    const host = $("#native-options");
    if (!host || !state.schema) return;
    const options = state.schema.native_options || [];
    if (!options.length) {
      host.innerHTML = '<p class="muted tiny">当前没有读到驱动原生选项。</p>';
      return;
    }
    host.innerHTML = options.map((option) => {
      const bound = option.bound_to
        ? `<span class="badge">已绑定 ${escapeHtml(option.bound_to)}</span>` : "";
      const choices = option.choices.map((choice) =>
        `<option value="${escapeHtml(choice.name)}" ${choice.name === option.default ? "selected" : ""}>${escapeHtml(choice.label || choice.name)}</option>`
      ).join("");
      return `<div class="native-item" title="${escapeHtml(option.label)}">
        <label>${escapeHtml(option.name)} ${bound}</label>
        <select data-native="${escapeHtml(option.name)}" ${option.bound_to ? "disabled" : ""}>
          <option value="">（不指定）</option>${choices}
        </select>
      </div>`;
    }).join("");
    $$("[data-native]", host).forEach((select) => {
      select.addEventListener("change", () => {
        const name = select.dataset.native;
        if (select.value) state.native[name] = select.value;
        else delete state.native[name];
      });
    });
  }

  function switchMode(mode, rerender = true) {
    state.mode = mode === "photo" ? "photo" : "document";
    if (state.mode === "photo") {
      // 照片模式下纸张尺寸跟随相纸，避免界面与提交内容不一致
      const photoMedia = getValue("photo.media");
      if (photoMedia) setValue("media", photoMedia);
      if (String(getValue("media_type") || "plain") === "plain") setValue("media_type", "photo");
    }
    $$(".mode-btn").forEach((button) =>
      button.classList.toggle("active", button.dataset.mode === state.mode));
    if (rerender) renderSettings();
  }

  // ------------------------------------------------------------ 文件上传
  function renderFiles() {
    const host = $("#file-list");
    const submit = $("#submit-job");
    if (!host) return;
    if (!state.files.length) {
      host.innerHTML = "";
      if (submit) submit.disabled = true;
      return;
    }
    host.innerHTML = state.files.map((entry) => {
      const thumb = entry.preview
        ? `<img class="file-thumb" src="${escapeHtml(entry.preview)}" alt="">`
        : `<div class="file-thumb placeholder">${entry.kind === "image" ? "🖼️" : entry.kind === "pdf" ? "📕" : "📄"}</div>`;
      const statusText = entry.status === "uploading" ? "上传中…"
        : entry.status === "ready" ? `${entry.upload.pages ? entry.upload.pages + " 页 · " : ""}${fmtBytes(entry.upload.bytes)}`
        : entry.status === "failed" ? (entry.error || "上传失败") : "等待上传";
      const statusClass = entry.status === "ready" ? "ok" : entry.status === "failed" ? "fail" : "";
      return `<li class="file-item">
        ${thumb}
        <div class="file-meta">
          <div class="file-name" title="${escapeHtml(entry.file.name)}">${escapeHtml(entry.file.name)}</div>
          <div class="file-sub file-status ${statusClass}">${escapeHtml(statusText)}</div>
        </div>
        ${entry.status === "uploading"
          ? '<span class="tiny muted">…</span>'
          : `<button class="icon-btn" type="button" data-remove="${entry.id}" title="移除">✕</button>`}
      </li>`;
    }).join("");

    $$("[data-remove]").forEach((button) => {
      button.addEventListener("click", () => {
        state.files = state.files.filter((item) => item.id !== button.dataset.remove);
        renderFiles();
      });
    });

    const ready = state.files.some((entry) => entry.status === "ready");
    if (submit) submit.disabled = !ready || state.submitting;
  }

  function uploadFile(entry) {
    return new Promise((resolve) => {
      const form = new FormData();
      form.append("file", entry.file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("X-CSRF-Token", CSRF);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        const node = document.querySelector(`[data-remove="${entry.id}"]`)?.closest(".file-item");
        if (node) node.querySelector(".file-status").textContent = `上传中 ${percent}%`;
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (err) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
          entry.status = "ready";
          entry.upload = data.upload;
          entry.kind = data.upload.kind;
          entry.preview = data.upload.preview_url || "";
          resolve(true);
        } else {
          entry.status = "failed";
          entry.error = data.error || `上传失败（HTTP ${xhr.status}）`;
          resolve(false);
        }
        renderFiles();
      };
      xhr.onerror = () => {
        entry.status = "failed";
        entry.error = "网络错误，上传失败";
        renderFiles();
        resolve(false);
      };
      xhr.send(form);
    });
  }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const limit = Number(state.limits.max_upload_mb || 64) * 1024 * 1024;
    for (const file of files) {
      if (file.size > limit) {
        toast(`${file.name} 超过 ${state.limits.max_upload_mb} MB 上限`, "fail");
        continue;
      }
      const entry = {
        id: `f${Date.now()}-${Math.random().toString(16).slice(2, 7)}`,
        file, status: "uploading", preview: "", upload: null, kind: "", error: "",
      };
      state.files.push(entry);
      renderFiles();
      await uploadFile(entry);
    }
    // 单张图片自动切到照片模式
    const ready = state.files.filter((item) => item.status === "ready");
    if (ready.length === 1 && ready[0].kind === "image" && state.mode !== "photo") {
      switchMode("photo");
    }
    renderFiles();
  }

  // ------------------------------------------------------------ 提交任务
  function buildSettings() {
    const payload = {};
    Object.entries(state.values).forEach(([key, value]) => {
      if (value === "" || value === null || value === undefined) return;
      payload[key] = value;
    });
    if (state.mode === "photo") {
      payload.media = getValue("photo.media") || payload.media;
      payload.media_type = payload.media_type || "photo";
      if (["draft", "normal"].includes(String(payload.quality))) payload.quality = "best";
    }
    if (Object.keys(state.native).length) payload.native = { ...state.native };
    return payload;
  }

  async function submitJobs() {
    const ready = state.files.filter((entry) => entry.status === "ready");
    if (!ready.length) {
      toast("请先选择并上传文件", "warn");
      return;
    }
    const button = $("#submit-job");
    state.submitting = true;
    if (button) { button.disabled = true; button.textContent = "提交中…"; }

    const settings = buildSettings();
    const titleInput = $("#job-title");
    const baseTitle = (titleInput?.value || "").trim();
    let created = 0;

    for (const entry of ready) {
      try {
        const payload = {
          upload_id: entry.upload.upload_id,
          mode: state.mode,
          settings,
          title: ready.length > 1
            ? `${baseTitle || entry.file.name.replace(/\.[^.]+$/, "")}`
            : baseTitle,
        };
        const data = await api("/api/jobs", { method: "POST", body: payload });
        created += 1;
        (data.warnings || []).forEach((warning) => toast(warning, "warn", 6000));
        entry.status = "submitted";
      } catch (err) {
        entry.status = "failed";
        entry.error = err.message;
        toast(`${entry.file.name}：${err.message}`, "fail", 7000);
      }
    }

    state.files = state.files.filter((entry) => entry.status !== "submitted" && entry.status !== "ready");
    if (titleInput) titleInput.value = "";
    state.submitting = false;
    if (button) { button.disabled = false; button.textContent = "开始打印"; }
    renderFiles();
    if (created) {
      toast(`已提交 ${created} 个打印任务`, "ok");
      pollJobs(true);
    }
  }

  // ------------------------------------------------------------ 任务列表
  function jobCard(job) {
    const badges = [];
    badges.push(`<span class="badge state-${escapeHtml(job.state)}">${escapeHtml(job.state_label)}</span>`);
    if (job.virtual) badges.push('<span class="badge warn">虚拟</span>');
    if (job.duplex && job.duplex !== "off") badges.push('<span class="badge">手动双面</span>');

    const meta = [
      job.settings_text,
      job.pages ? `${job.pages} 页` : "",
      job.created_at ? fmtTime(job.created_at) : "",
    ].filter(Boolean).map(escapeHtml).join(" · ");

    const actions = [];
    if (job.can_continue) {
      actions.push(`<button class="btn btn-continue" type="button" data-continue="${escapeHtml(job.id)}">
        ✅ 我已翻面，继续打印</button>`);
    }
    if (job.can_cancel) actions.push(`<button class="btn tiny ghost" type="button" data-cancel="${escapeHtml(job.id)}">取消</button>`);
    if (job.can_retry) actions.push(`<button class="btn tiny ghost" type="button" data-retry="${escapeHtml(job.id)}">重试</button>`);
    actions.push(`<button class="btn tiny ghost" type="button" data-preview="${escapeHtml(job.id)}">预览</button>`);
    if (job.pass1_pages) {
      actions.push(`<a class="btn tiny ghost" href="/api/jobs/${encodeURIComponent(job.id)}/file/pass1">下载第1面</a>`);
    }

    const command = job.command1
      ? `<details class="job-cmd-wrap"><summary class="tiny muted">投递命令</summary>
           <div class="job-cmd">${escapeHtml(job.command1)}</div>
           ${job.command2 ? `<div class="job-cmd">${escapeHtml(job.command2)}</div>` : ""}
         </details>`
      : "";

    const msg = job.error
      ? `<div class="job-msg error">${escapeHtml(job.error)}</div>`
      : job.message ? `<div class="job-msg muted">${escapeHtml(job.message)}</div>` : "";

    const busy = job.busy ? '<div class="progress"><i></i></div>' : "";

    return `<article class="job ${job.can_continue ? "awaiting" : ""} ${job.state === "failed" ? "failed" : ""}"
        data-job="${escapeHtml(job.id)}">
      <div class="job-head">
        <span class="job-title" title="${escapeHtml(job.title)}">${escapeHtml(job.title)}</span>
        ${badges.join(" ")}
      </div>
      <div class="job-meta">${meta}</div>
      ${msg}${busy}
      <div class="job-actions">${actions.join("")}</div>
      ${command}
    </article>`;
  }

  function renderJobs() {
    const host = $("#jobs");
    if (!host) return;
    const summary = $("#jobs-summary");
    if (!state.jobs.length) {
      host.innerHTML = '<p class="muted center empty">还没有任务</p>';
      if (summary) summary.textContent = "";
      return;
    }
    host.innerHTML = state.jobs.map(jobCard).join("");
    if (summary) {
      const active = state.jobs.filter((job) => job.busy || job.can_continue).length;
      summary.textContent = `${state.jobs.length} 个任务${active ? ` · ${active} 个进行中` : ""}`;
    }
    bindJobActions(host);
    maybeOpenDuplexModal();
  }

  function bindJobActions(root) {
    $$("[data-continue]", root).forEach((button) => {
      button.addEventListener("click", () => continueJob(button.dataset.continue, button));
    });
    $$("[data-cancel]", root).forEach((button) => {
      button.addEventListener("click", () => cancelJob(button.dataset.cancel));
    });
    $$("[data-retry]", root).forEach((button) => {
      button.addEventListener("click", () => retryJob(button.dataset.retry));
    });
    $$("[data-preview]", root).forEach((button) => {
      button.addEventListener("click", () => {
        window.open(`/api/jobs/${encodeURIComponent(button.dataset.preview)}/preview`, "_blank");
      });
    });
  }

  async function pollJobs(force = false) {
    try {
      const data = await api(`/api/jobs?limit=30${force ? "&t=" + Date.now() : ""}`);
      state.jobs = data.jobs || [];
      renderJobs();
      renderStatus(await api("/api/status"));
    } catch (err) {
      if (!String(err.message).includes("未登录")) {
        console.warn("刷新任务失败", err.message);
      }
    }
  }

  // ------------------------------------------------------------ 手动双面弹窗
  function maybeOpenDuplexModal() {
    const pending = state.jobs.find((job) => job.can_continue);
    if (!pending) {
      if (state.modalJob && !state.jobs.some((job) => job.id === state.modalJob && job.can_continue)) {
        closeModal();
      }
      return;
    }
    if (state.modalJob === pending.id) return;
    if (state.seenFlip.has(pending.id)) return;
    openDuplexModal(pending);
  }

  function openDuplexModal(job) {
    const modal = $("#duplex-modal");
    if (!modal) return;
    state.modalJob = job.id;
    state.seenFlip.add(job.id);
    modal.dataset.kind = "flip";
    const text = $("#duplex-text");
    if (text) {
      text.innerHTML = `任务 <b>${escapeHtml(job.title)}</b> 的第一面（奇数页 ${job.pass1_pages} 页）已送打印。<br>
        等打印机停稳、纸张全部输出后，${job.duplex === "short-edge" ? "沿短边" : "沿长边"}把整叠纸翻面，
        再点下面的按钮。`;
    }
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    const button = $("#duplex-continue");
    if (button) { button.disabled = false; button.textContent = "✅ 我已翻面，继续打印"; }
  }

  function closeModal() {
    const modal = $("#duplex-modal");
    if (!modal) return;
    modal.hidden = true;
    modal.removeAttribute("data-kind");
    state.modalJob = null;
    document.body.style.overflow = "";
  }

  async function continueJob(jobId, button) {
    const target = button || $("#duplex-continue");
    if (target) { target.disabled = true; target.textContent = "正在送打印…"; }
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/continue`, { method: "POST" });
      toast("已开始打印第二面", "ok");
      closeModal();
      await pollJobs(true);
    } catch (err) {
      toast(err.message, "fail", 6000);
      if (target) { target.disabled = false; target.textContent = "✅ 我已翻面，继续打印"; }
      await pollJobs(true);
    }
  }

  async function cancelJob(jobId) {
    if (!window.confirm("确定取消这个任务？第二面将不会再打印。")) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
      toast("已取消", "ok", 2000);
      closeModal();
      await pollJobs(true);
    } catch (err) {
      toast(err.message, "fail");
    }
  }

  async function retryJob(jobId) {
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
      toast("已重新排队", "ok", 2000);
      await pollJobs(true);
    } catch (err) {
      toast(err.message, "fail");
    }
  }

  // ------------------------------------------------------------ 打印机管理（设置页）
  function suggestQueueName(uri) {
    let host = "";
    try {
      host = new URL(uri).hostname || "";
    } catch (err) {
      host = (String(uri).split("//")[1] || "").split("/")[0] || "";
    }
    const base = host.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "PRINTER";
    return `NET-${base}`.slice(0, 60);
  }

  async function loadPrinterManager() {
    const host = $("#printer-manager");
    if (!host) return;
    host.innerHTML = '<p class="muted tiny">正在读取 CUPS 队列…</p>';
    try {
      renderPrinterManager(await api("/api/printers"));
    } catch (err) {
      host.innerHTML = `<p class="job-msg error">${escapeHtml(err.message)}</p>`;
    }
  }

  function renderPrinterManager(data) {
    const host = $("#printer-manager");
    if (!host) return;
    const selected = data.selected || "";
    const label = $("#printer-selected");
    if (label) {
      label.innerHTML = selected
        ? `当前使用：<b>${escapeHtml(selected)}</b>`
        : "未选择队列";
    }

    const queues = data.queues || [];
    const rows = queues.map((queue) => {
      const uri = queue.device_uri || "";
      const isNetwork = /^(ipp|ipps|dnssd|socket|lpd|hp:\/net)/i.test(uri);
      const isSelected = queue.name === selected;
      const badges = [
        `<span class="badge ${isNetwork ? "ok" : ""}">${isNetwork ? "网络" : uri ? "USB" : "未知"}</span>`,
      ];
      if (isSelected) badges.push('<span class="badge admin">使用中</span>');
      if (!queue.enabled) badges.push('<span class="badge warn">停用</span>');
      if (!queue.accepting) badges.push('<span class="badge warn">暂停</span>');
      return `<tr>
        <td class="strong nowrap">${escapeHtml(queue.name)}</td>
        <td>${badges.join(" ")}<div class="tiny muted mono">${escapeHtml(uri || "—")}</div></td>
        <td class="nowrap">
          ${isSelected ? "" : `<button class="btn tiny ghost" type="button" data-use-printer="${escapeHtml(queue.name)}">使用</button>`}
          <button class="btn tiny ghost" type="button" data-test-printer="${escapeHtml(queue.name)}">打测试页</button>
          ${isSelected ? "" : `<button class="btn tiny danger" type="button" data-del-printer="${escapeHtml(queue.name)}">删除</button>`}
        </td>
      </tr>`;
    }).join("");

    host.innerHTML = `
      ${data.can_manage ? "" : `<p class="alert error">${escapeHtml(data.manage_hint || "没有管理打印机的权限")}</p>`}
      ${queues.length
        ? `<div class="table-wrap"><table class="table compact">
             <thead><tr><th>队列</th><th>连接</th><th>操作</th></tr></thead>
             <tbody>${rows}</tbody></table></div>`
        : `<p class="muted tiny">CUPS 里还没有队列。插好 USB 跑一次
             <code>scripts/setup-usb-printer.sh</code>，或在下面添加网络打印机。</p>`}

      <div class="row gap">
        <button class="btn ghost" id="discover-printers" type="button">🔍 扫描局域网打印机</button>
        <span class="tiny muted">mDNS / lpinfo 探测同一网段的 AirPrint / IPP 打印机</span>
      </div>
      <div id="discovered-printers" class="discovered"></div>

      <details class="native-box">
        <summary>手动添加网络打印机</summary>
        <form id="manual-printer-form" class="manual-printer">
          <label class="field"><span>打印机地址</span>
            <input type="text" name="uri" placeholder="ipp://192.168.1.50/ipp/print" required>
          </label>
          <label class="field"><span>队列名</span>
            <input type="text" name="name" placeholder="HP4928-NET" required>
          </label>
          <label class="field"><span>驱动（留空 = IPP Everywhere）</span>
            <input type="text" name="model" placeholder="drv:///hpcups.drv/hp-deskjet_4900_series.ppd">
          </label>
          <button class="btn primary" type="submit">添加并设为默认</button>
        </form>
        <p class="hint tiny">
          地址一般是 <code>ipp://打印机IP/ipp/print</code>（AirPrint/IPP 机型）。
          老机型不支持 IPP Everywhere 时，在「驱动」里填 PPD，
          例如 <code>drv:///hpcups.drv/hp-deskjet_4900_series.ppd</code>。
        </p>
      </details>`;

    bindPrinterActions();
  }

  function bindPrinterActions() {
    $$("[data-use-printer]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await api("/api/printers/select", {
            method: "POST", body: { name: button.dataset.usePrinter },
          });
          toast(`已切换到 ${button.dataset.usePrinter}`, "ok");
          setTimeout(() => window.location.reload(), 500);
        } catch (err) {
          toast(err.message, "fail");
        }
      });
    });

    $$("[data-test-printer]").forEach((button) => {
      button.addEventListener("click", async () => {
        const name = button.dataset.testPrinter;
        button.disabled = true;
        try {
          const data = await api("/api/printers/test", { method: "POST", body: { name } });
          toast(`测试页已送 ${name}：${data.message || "已投递"}`, "ok", 8000);
        } catch (err) {
          toast(err.message, "fail", 9000);
        } finally {
          button.disabled = false;
        }
      });
    });

    $$("[data-del-printer]").forEach((button) => {
      button.addEventListener("click", async () => {
        const name = button.dataset.delPrinter;
        if (!window.confirm(`删除 CUPS 队列 ${name}？（不影响打印机本身）`)) return;
        try {
          await api(`/api/printers/${encodeURIComponent(name)}`, { method: "DELETE" });
          toast(`已删除 ${name}`, "ok");
          setTimeout(() => window.location.reload(), 500);
        } catch (err) {
          toast(err.message, "fail", 9000);
        }
      });
    });

    $("#discover-printers")?.addEventListener("click", discoverPrinters);

    $("#manual-printer-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      addPrinter(form.uri.value.trim(), form.name.value.trim(), form.model.value.trim());
    });
  }

  async function discoverPrinters() {
    const host = $("#discovered-printers");
    if (!host) return;
    host.innerHTML = '<p class="muted tiny">正在扫描（约 5 秒）…</p>';
    try {
      const data = await api("/api/printers/discover");
      (data.notes || []).forEach((note) => toast(note, "warn", 8000));
      const items = data.printers || [];
      if (!items.length) {
        host.innerHTML = '<p class="muted tiny">没有发现。可在下面手动填地址添加。</p>';
        return;
      }
      host.innerHTML = items.map((item) => `
        <div class="discovered-row">
          <div class="grow">
            <div class="strong tiny">${escapeHtml(item.name || item.uri)}</div>
            <div class="tiny muted mono">${escapeHtml(item.uri)}</div>
          </div>
          <input class="input" data-name-for="${escapeHtml(item.uri)}"
                 value="${escapeHtml(suggestQueueName(item.uri))}">
          <button class="btn tiny primary" type="button"
                  data-add-discovered="${escapeHtml(item.uri)}">添加</button>
        </div>`).join("");
      $$("[data-add-discovered]").forEach((button) => {
        button.addEventListener("click", () => {
          const uri = button.dataset.addDiscovered;
          const input = document.querySelector(`[data-name-for="${CSS.escape(uri)}"]`);
          addPrinter(uri, input ? input.value.trim() : "");
        });
      });
    } catch (err) {
      host.innerHTML = `<p class="job-msg error">${escapeHtml(err.message)}</p>`;
    }
  }

  async function addPrinter(uri, name, model = "") {
    if (!uri || !name) {
      toast("地址和队列名都要填", "warn");
      return;
    }
    try {
      const data = await api("/api/printers/add", {
        method: "POST", body: { uri, name, model, select: true },
      });
      toast(data.message || "已添加", "ok");
      setTimeout(() => window.location.reload(), 600);
    } catch (err) {
      toast(err.message, "fail", 12000);
    }
  }

  // ------------------------------------------------------------ 页面初始化
  function initIndex() {
    renderStatus(state.status);
    renderPresets();
    renderSettings();
    renderNative();
    renderFiles();
    renderJobs();

    const dropzone = $("#dropzone");
    const input = $("#file-input");
    dropzone?.addEventListener("click", () => input?.click());
    dropzone?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input?.click(); }
    });
    input?.addEventListener("change", (event) => {
      handleFiles(event.target.files);
      event.target.value = "";
    });
    ["dragenter", "dragover"].forEach((name) => {
      dropzone?.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropzone?.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone?.addEventListener("drop", (event) => handleFiles(event.dataTransfer?.files));

    $$(".mode-btn").forEach((button) => {
      button.addEventListener("click", () => switchMode(button.dataset.mode));
    });

    $("#submit-job")?.addEventListener("click", submitJobs);
    $("#refresh-jobs")?.addEventListener("click", () => pollJobs(true));
    $("#refresh-status")?.addEventListener("click", () => pollJobs(true));

    const continueButton = $("#duplex-continue");
    continueButton?.addEventListener("click", () => {
      if (state.modalJob) continueJob(state.modalJob, continueButton);
    });
    $("#duplex-cancel")?.addEventListener("click", () => {
      if (state.modalJob) cancelJob(state.modalJob);
    });

    state.pollTimer = setInterval(pollJobs, 2600);
    pollJobs();
  }

  function initHistory() {
    const root = document;
    bindJobActions(root);
    $$("[data-continue]").forEach((button) => {
      button.onclick = () => continueJob(button.dataset.continue, button);
    });
    $$("[data-cancel]").forEach((button) => {
      button.onclick = () => cancelJob(button.dataset.cancel);
    });
    $$("[data-retry]").forEach((button) => {
      button.onclick = () => retryJob(button.dataset.retry);
    });
  }

  function initSettings() {
    loadPrinterManager();

    $("#refresh-schema")?.addEventListener("click", async () => {
      try {
        await api("/api/schema?refresh=1");
        toast("已重新读取驱动选项", "ok");
        setTimeout(() => window.location.reload(), 700);
      } catch (err) {
        toast(err.message, "fail");
      }
    });
  }

  // ------------------------------------------------------------ 个人中心
  function initAccount() {
    $("#password-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const current = form.current_password.value;
      const next = form.new_password.value;
      const confirm = form.confirm.value;
      if (!current) { toast("请先输入当前密码", "warn"); return; }
      if (next.length < 8) { toast("新密码至少 8 位", "warn"); return; }
      if (next !== confirm) { toast("两次输入的新密码不一致", "fail"); return; }
      try {
        await api("/api/auth/password", {
          method: "POST",
          body: { current_password: current, new_password: next },
        });
        toast("密码已更新", "ok");
        form.reset();
      } catch (err) {
        toast(err.message, "fail");
      }
    });
  }

  // ------------------------------------------------------------ 用户权限
  function initUsers() {
    $("#new-user-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      try {
        const data = await api("/api/users", {
          method: "POST",
          body: { username: form.username.value.trim(), role: form.role.value, generate: true },
        });
        toast(`已创建 ${data.user.username}，初始密码：${data.password}（请立即保存）`, "ok", 12000);
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) {
        toast(err.message, "fail");
      }
    });

    $$("[data-reset-password]").forEach((button) => {
      button.addEventListener("click", async () => {
        const username = button.dataset.resetPassword;
        if (!window.confirm(`重置 ${username} 的密码？`)) return;
        try {
          const data = await api(`/api/users/${encodeURIComponent(username)}/password`, {
            method: "POST", body: { generate: true },
          });
          toast(`${username} 的新密码：${data.password}（只显示一次）`, "ok", 15000);
        } catch (err) {
          toast(err.message, "fail");
        }
      });
    });

    $$("[data-toggle-user]").forEach((button) => {
      button.addEventListener("click", async () => {
        const username = button.dataset.toggleUser;
        const disabled = button.dataset.disabled === "1";
        const action = disabled ? "启用" : "停用";
        if (!window.confirm(`${action}账号 ${username}？`)) return;
        try {
          await api(`/api/users/${encodeURIComponent(username)}/disable`, {
            method: "POST", body: { disabled: !disabled },
          });
          toast(`已${action}`, "ok");
          setTimeout(() => window.location.reload(), 600);
        } catch (err) {
          toast(err.message, "fail");
        }
      });
    });

    $$("[data-delete-user]").forEach((button) => {
      button.addEventListener("click", async () => {
        const username = button.dataset.deleteUser;
        if (!window.confirm(`删除账号 ${username}？该账号将无法再登录。`)) return;
        try {
          await api(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
          toast("已删除", "ok");
          setTimeout(() => window.location.reload(), 700);
        } catch (err) {
          toast(err.message, "fail");
        }
      });
    });
  }

  // ------------------------------------------------------------ 消息通知
  function initNotifications() {
    $$("[data-continue]").forEach((button) => {
      button.addEventListener("click", async () => {
        const jobId = button.dataset.continue;
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "正在送打印…";
        try {
          await api(`/api/jobs/${encodeURIComponent(jobId)}/continue`, { method: "POST" });
          toast("已开始打印第二面", "ok");
          setTimeout(() => window.location.reload(), 900);
        } catch (err) {
          toast(err.message, "fail", 6000);
          button.disabled = false;
          button.textContent = original;
        }
      });
    });

    $$("[data-retry]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/api/jobs/${encodeURIComponent(button.dataset.retry)}/retry`, {
            method: "POST",
          });
          toast("已重新排队", "ok");
          setTimeout(() => window.location.reload(), 900);
        } catch (err) {
          toast(err.message, "fail");
          button.disabled = false;
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (currentPage === "index") initIndex();
    else if (currentPage === "history") initHistory();
    else if (currentPage === "settings") initSettings();
    else if (currentPage === "account") initAccount();
    else if (currentPage === "users") initUsers();
    else if (currentPage === "notifications") initNotifications();
  });

  // 页面隐藏时停止轮询，避免手机耗电
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    } else if (currentPage === "index" && !state.pollTimer) {
      pollJobs(true);
      state.pollTimer = setInterval(pollJobs, 2600);
    }
  });
})();
