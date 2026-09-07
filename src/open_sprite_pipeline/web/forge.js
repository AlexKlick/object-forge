import { api, toast, escapeHtml as escapeText } from "./app.js";
import { MeshViewer, loadViewerModules } from "./viewer.js";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => escapeText(String(value)).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const forge = "/v1/forge";
const encode = encodeURIComponent;
const json = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const versionPath = (v) => `${forge}/assets/${encode(v.asset)}/variants/${encode(v.variant)}/versions/${v.number}`;
const artifactPath = (v, name) => `${versionPath(v)}/artifacts/${name.split("/").map(encode).join("/")}`;
const lineage = (v) => v.lineage?.parent_version ? `v${v.number} ← v${v.lineage.parent_version}` : `v${v.number} · root`;
const pretty = (value) => esc(JSON.stringify(value, null, 2));
const button = (text, action, style = "secondary") => `<button type="button" class="button ${style}" data-action="${action}">${esc(text)}</button>`;
const criticBadge = (status = "pending") => `<span class="forge-badge critic-${["pass", "warn", "fail"].includes(status) ? status : "neutral"}">Critic: ${esc(status)}</span>`;
const guard = (fn) => async (...args) => { try { await fn(...args); } catch (error) { toast(error.message, true); } };

function showTab(name) {
  for (const tab of document.querySelectorAll("[data-main-tab]")) {
    tab.setAttribute("aria-selected", String(tab.dataset.mainTab === name));
  }
  for (const panel of document.querySelectorAll("[data-main-panel]")) panel.hidden = panel.dataset.mainPanel !== name;
  if (name === "library") library.refresh().catch((error) => toast(error.message, true));
  if (name === "forge") board.refreshPickers().catch((error) => toast(error.message, true));
}

// Shared click/drop control: local object URLs are released when a file is removed.
function dropzone(container, changed, { multiple = true, title = "Drop sheets here or click to browse" } = {}) {
  let items = [];
  container.innerHTML = `<label class="forge-drop"><strong>${esc(title)}</strong><small>PNG, JPEG, WebP, GIF · up to 8 images, 20 MiB each</small><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" ${multiple ? "multiple" : ""}></label><div class="forge-thumbnails"></div>`;
  const input = $("input", container);
  const render = () => {
    $(".forge-thumbnails", container).innerHTML = items.map((item, index) => `<figure><img src="${item.url}" alt="${esc(item.file.name)}"><figcaption>${esc(item.file.name)}</figcaption><button type="button" class="button ghost" data-remove="${index}" aria-label="Remove ${esc(item.file.name)}">Remove</button></figure>`).join("");
    changed(items.map((item) => item.file));
  };
  const clear = () => { items.forEach((item) => URL.revokeObjectURL(item.url)); items = []; render(); };
  const add = (files) => {
    const selected = Array.from(files || []);
    if (selected.some((file) => !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.type) || !file.size || file.size > 20 * 1024 * 1024)) {
      toast("Choose nonempty images of at most 20 MiB each.", true); return;
    }
    if (multiple && items.length + selected.length > 8) { toast("Upload at most eight images.", true); return; }
    if (!multiple) clear();
    items.push(...(multiple ? selected : selected.slice(0, 1)).map((file) => ({ file, url: URL.createObjectURL(file) })));
    render();
  };
  input.addEventListener("change", () => { add(input.files); input.value = ""; });
  container.addEventListener("dragover", (event) => { event.preventDefault(); container.classList.add("dragging"); });
  container.addEventListener("dragleave", () => container.classList.remove("dragging"));
  container.addEventListener("drop", (event) => { event.preventDefault(); container.classList.remove("dragging"); add(event.dataTransfer.files); });
  container.addEventListener("click", (event) => {
    const target = event.target.closest("[data-remove]");
    if (!target) return;
    URL.revokeObjectURL(items.splice(Number(target.dataset.remove), 1)[0].url); render();
  });
  return { clear };
}

class BoardView {
  constructor(root) {
    this.root = root;
    this.files = [];
    this.segmentRefs = new Map();
    this.replacements = new Map();
    this.slots = [];
    this.parents = [];
    this.jobs = new Map();
    this.parentRequest = 0;
    root.innerHTML = `<div class="forge-heading"><div><p class="eyebrow">Sheets → review → bake</p><h1>Bake Forge</h1></div><span id="forgeStatus" role="status">Checking worker store…</span></div>
      <form id="forgeForm" class="forge-surface">
        <div class="forge-fields"><label>Asset<input name="asset" required class="text-control" list="forgeAssets" placeholder="asset id"></label><datalist id="forgeAssets"></datalist>
        <label>Variant<input name="variant" required class="text-control" list="forgeVariants" placeholder="variant id"></label><datalist id="forgeVariants"></datalist></div>
        <label class="forge-check"><input id="iterateToggle" type="checkbox"> Iterate an existing version</label>
        <label class="forge-check"><input id="generateToggle" type="checkbox"> Generate blockout from photos</label>
        <div id="generateFields" hidden><div class="forge-fields">
          <label>Height hint (m)<input id="generateHeightHint" type="number" min="1" max="300" step="any" value="12" class="text-control"></label>
          <label>Floor height (m)<input id="generateFloorHeight" type="number" min="1" max="10" step="any" value="3" class="text-control"></label></div>
          <p>Choose one to seven photos and capture cutouts in total.</p>
          <button id="generateLatestCutouts" type="button" class="button secondary">Use latest capture cutouts</button>
          <div id="generateCutouts" class="forge-fields"></div></div>
        <div id="iterateFields" hidden><div class="forge-fields"><label>Parent version<select id="parentVersion" class="select-control"><option value="">Choose a library version</option></select></label>
        <label>Intent<select id="iterateIntent" class="select-control"><option value="iterate_views">iterate_views</option><option value="iterate_params">iterate_params</option></select></label></div><div id="replacementSlots" class="forge-slots"></div></div>
        <div id="freshUploads"></div>
        <details class="forge-advanced"><summary>Advanced parameters</summary><div class="forge-fields">
          ${[["iou", .70], ["margin", .05], ["ownership_min", .5], ["view_iou_warn", .90], ["view_iou_fail", ""], ["atlas_tile", 1024], ["turntable", 8]].map(([name, value]) => `<label>${name}<input name="${name}" type="number" class="text-control" min="${name === "atlas_tile" ? 1 : 0}" ${["atlas_tile", "turntable"].includes(name) ? 'step="1"' : 'max="1" step="0.01"'} value="${value}" ${name === "view_iou_fail" ? 'placeholder="disabled"' : "required"}></label>`).join("")}
          <label class="forge-check"><input name="allow_extra" type="checkbox"> allow_extra</label></div></details>
        <button class="button primary" type="submit">Create job →</button><p id="forgeFormError" class="forge-error" role="alert"></p>
      </form><div id="forgeJobs" class="forge-jobs" aria-live="polite"></div>`;
    this.uploads = dropzone($("#freshUploads"), (files) => { this.files = files; });
    $("#iterateToggle").addEventListener("change", () => {
      if ($("#iterateToggle").checked) $("#generateToggle").checked = false;
      this.mode();
    });
    $("#generateToggle").addEventListener("change", () => {
      if ($("#generateToggle").checked) {
        $("#iterateToggle").checked = false;
        if (!$('[name=variant]', root).value) $('[name=variant]', root).value = "default";
      }
      this.mode();
    });
    $("#generateLatestCutouts").onclick = guard(() => this.latestCutouts());
    $("#iterateIntent").addEventListener("change", () => this.mode());
    $("#parentVersion").addEventListener("change", guard(() => this.selectParent()));
    $("[name=asset]", root).addEventListener("input", () => this.variants());
    $("#forgeForm").addEventListener("submit", (event) => { event.preventDefault(); this.create(); });
    this.start();
  }

  async start() {
    try {
      const status = await api(`${forge}/status`);
      $("#forgeStatus").textContent = status.enabled ? "Forge enabled" : "Forge disabled";
      await this.refreshPickers();
      for (const job of await api(`${forge}/jobs`)) this.card(job);
    } catch (error) { $("#forgeStatus").textContent = `Forge unavailable: ${error.message}`; }
    // Serialized timeout polling avoids overlapping responses and duplicate log lines.
    const poll = async () => {
      try {
        for (const [id, card] of this.jobs) {
          if (!["ready", "failed"].includes(card.job.state)) this.card(await api(`${forge}/jobs/${id}`));
        }
      } catch (error) { $("#forgeStatus").textContent = `Polling error: ${error.message}`; }
      this.pollTimer = setTimeout(poll, 2000);
    };
    this.pollTimer = setTimeout(poll, 2000);
  }

  async refreshPickers() {
    [this.assets, this.parents] = await Promise.all([api(`${forge}/assets`), api(`${forge}/library`)]);
    this.parents = this.parents.filter((v) => v.origin !== "trellis" && v.job_id);
    $("#forgeAssets").innerHTML = [...new Set(this.assets.map((entry) => entry.asset))].map((asset) => `<option value="${esc(asset)}"></option>`).join("");
    this.variants();
    const selected = $("#parentVersion").value;
    $("#parentVersion").innerHTML = '<option value="">Choose a library version</option>' + this.parents.map((v) => `<option value="${esc(v.job_id)}">${esc(v.asset)} / ${esc(v.variant)} · ${lineage(v)}</option>`).join("");
    $("#parentVersion").value = selected;
  }

  variants() {
    const asset = $("[name=asset]", this.root).value;
    $("#forgeVariants").innerHTML = (this.assets || []).filter((entry) => entry.asset === asset).map((entry) => `<option value="${esc(entry.variant)}"></option>`).join("");
  }

  mode() {
    const iterate = $("#iterateToggle").checked;
    $("#iterateFields").hidden = !iterate;
    $("#generateFields").hidden = !$("#generateToggle").checked;
    $("#freshUploads").hidden = iterate;
    $("#replacementSlots").hidden = $("#iterateIntent").value !== "iterate_views";
    for (const name of ["asset", "variant"]) $(`[name=${name}]`, this.root).readOnly = iterate;
  }

  async latestCutouts() {
    const control = $("#generateLatestCutouts"); control.disabled = true;
    try {
      const { runs } = await api("/v1/ui/runs?limit=5");
      const container = $("#generateCutouts"); container.replaceChildren();
      this.segmentRefs.clear();
      const seen = new Set();
      for (const run of runs.filter((run) => run.status === "completed" && run.image_id && run.segment_id)) {
        const ref = { image_id: run.image_id, segment_id: run.segment_id };
        const key = JSON.stringify(ref); if (seen.has(key)) continue; seen.add(key);
        const label = document.createElement("label"); label.className = "forge-check";
        label.innerHTML = `<input type="checkbox"><img width="96" height="96" style="object-fit:contain" src="/v1/ui/uploads/${encode(ref.image_id)}/segments/${encode(ref.segment_id)}/cutout" alt="Capture cutout"> ${esc(run.record_key)}`;
        $("input", label).onchange = (event) => {
          if (event.target.checked) this.segmentRefs.set(key, ref); else this.segmentRefs.delete(key);
        };
        container.append(label);
      }
      if (!container.childElementCount) container.textContent = "No completed capture cutouts in the latest five runs.";
    } finally { control.disabled = false; }
  }

  async selectParent() {
    const request = ++this.parentRequest;
    this.parent = null;
    this.slots.forEach((slot) => slot.clear()); this.slots = []; this.replacements.clear();
    $("#replacementSlots").replaceChildren();
    const version = this.parents.find((v) => v.job_id === $("#parentVersion").value);
    if (!version) return;
    const job = await api(`${forge}/jobs/${version.job_id}`);
    if (request !== this.parentRequest) return;
    this.parent = version;
    for (const name of ["asset", "variant"]) $(`[name=${name}]`, this.root).value = version[name];
    for (const [name, value] of Object.entries(job.params)) {
      const field = $(`[name=${name}]`, this.root);
      if (field?.type === "checkbox") field.checked = value;
      else if (field) field.value = value ?? "";
    }
    for (const view of job.canonical_views) {
      const slot = document.createElement("div"); $("#replacementSlots").append(slot);
      this.slots.push(dropzone(slot, (files) => {
        if (files.length) this.replacements.set(view, files[0]); else this.replacements.delete(view);
      }, { multiple: false, title: `Replace ${view}?` }));
    }
    this.mode();
  }

  async iterate(version) {
    showTab("forge");
    await this.refreshPickers();
    $("#iterateToggle").checked = true;
    $("#generateToggle").checked = false;
    $("#parentVersion").value = version.job_id;
    await this.selectParent();
    this.root.scrollIntoView({ behavior: "smooth" });
  }

  async create() {
    const submit = $("button[type=submit]", this.root);
    submit.disabled = true; $("#forgeFormError").textContent = "";
    try {
      const iterate = $("#iterateToggle").checked;
      const intent = iterate ? $("#iterateIntent").value : $("#generateToggle").checked ? "generate" : "fresh";
      if (iterate && !this.parent) throw new Error("Choose a parent version first.");
      const pairs = intent === "iterate_views" ? [...this.replacements] : [];
      const files = iterate ? pairs.map(([, file]) => file) : this.files;
      const refs = intent === "generate" ? [...this.segmentRefs.values()] : [];
      if ((!files.length && !refs.length && intent !== "iterate_params") || files.length > 8) throw new Error("Choose between one and eight images.");
      if (intent === "generate" && files.length + refs.length > 7) throw new Error("Choose at most seven photos/cutouts in total.");
      const body = new FormData();
      for (const name of ["asset", "variant"]) body.append(name, $(`[name=${name}]`, this.root).value.trim());
      body.append("intent", intent);
      if (intent === "generate") {
        body.append("segment_refs", JSON.stringify(refs));
        body.append("height_hint", $("#generateHeightHint").value);
        body.append("floor_height", $("#generateFloorHeight").value);
      }
      const params = {};
      for (const input of this.root.querySelectorAll(".forge-advanced input")) params[input.name] = input.type === "checkbox" ? input.checked : input.value === "" ? null : Number(input.value);
      body.append("params", JSON.stringify(params));
      if (iterate) {
        body.append("parent_job", this.parent.job_id); body.append("parent_version", this.parent.number);
        body.append("replacement_views", JSON.stringify(pairs.map(([view]) => view)));
      }
      for (const file of files) body.append("files", file);
      const job = await api(`${forge}/jobs`, { method: "POST", body });
      this.card(job); toast("Job created.");
    } catch (error) { $("#forgeFormError").textContent = error.message; }
    finally { submit.disabled = false; }
  }

  card(job) {
    let card = this.jobs.get(job.id);
    if (!card) {
      const root = document.createElement("article"); root.className = "forge-surface forge-job";
      root.innerHTML = `<div class="forge-heading"><h2>${esc(job.asset)} / ${esc(job.variant)}</h2><span class="forge-state"></span></div><small>${esc(job.id)} · ${esc(job.intent)}</small><pre class="forge-log" aria-label="Worker marker log"></pre><p class="forge-error" role="alert"></p><div class="forge-job-actions"></div><div class="forge-blockout-review" hidden></div><div class="forge-review" hidden></div>`;
      $("#forgeJobs").prepend(root); card = { root, log: [], job }; this.jobs.set(job.id, card);
    }
    card.job = job;
    const blockoutRoot = $(".forge-blockout-review", card.root);
    blockoutRoot.hidden = !["generate", "iterate_blockout"].includes(job.intent) || job.state !== "review" || !job.generate?.blockout;
    const revision = `${job.generate?.regenerations}:${job.state}:${!!job.match.submitted}`;
    if (card.revision !== revision) {
      card.review = null;
      if (!blockoutRoot.hidden) card.blockout = new BlockoutReview(blockoutRoot, job, (updated) => this.card(updated), () => card.review);
      card.revision = revision;
    }
    const chip = $(".forge-state", card.root); chip.textContent = job.state; chip.dataset.state = job.state;
    const lines = job.worker_log || [];
    // The API retains a rolling tail. Match its largest overlap to append only new lines.
    let overlap = Math.min(card.log.length, lines.length);
    while (overlap && card.log.slice(-overlap).join("\n") !== lines.slice(0, overlap).join("\n")) overlap--;
    if (lines.length > overlap) {
      const log = $(".forge-log", card.root);
      log.append(document.createTextNode(lines.slice(overlap).join("\n") + "\n")); log.scrollTop = log.scrollHeight;
    }
    card.log = lines;
    $(".forge-error", card.root).textContent = job.error || "";
    const actions = $(".forge-job-actions", card.root);
    actions.innerHTML = job.state === "review" ? job.match.submitted ? "Review submitted · staging views…" : button("Review matches", "review", "primary")
      : job.state === "staged" ? button("Approve bake", "approve", "accent")
        : job.state === "ready" ? button(`Open v${job.version_number}`, "version") : "";
    actions.onclick = guard(async (event) => {
      const action = event.target.closest("[data-action]")?.dataset.action;
      if (action === "review") {
        if (!card.review) card.review = new MatchReview($(".forge-review", card.root), job, (updated) => this.card(updated));
        $(".forge-review", card.root).hidden = false;
      }
      if (action === "approve") this.card(await api(`${forge}/jobs/${job.id}/approve`, { method: "POST" }));
      if (action === "version") { showTab("library"); await detail.open({ ...job, number: job.version_number }); }
    });
    if (job.state !== "review" || job.match.submitted) {
      $(".forge-review", card.root).hidden = true;
      card.review = null;
    }
  }
}

class BlockoutReview {
  constructor(root, job, updated, matchReview) {
    this.root = root; this.job = job;
    const blockout = job.generate.blockout;
    const id = `blockoutReview-${job.id}`;
    root.id = id;
    root.innerHTML = `<h3>Blockout review</h3>
      <div id="${id}-renders" class="blockout-renders"><img style="max-width:100%;max-height:360px" alt="Blockout render"><p class="blockout-view-name"></p><div class="button-row">${button("Previous render", "previous")}${button("Next render", "next")}</div></div>
      <table class="blockout-params"><tbody>${Object.entries(blockout.params).map(([name, value]) => `<tr><th>${esc(name)}</th><td>${esc(typeof value === "object" ? JSON.stringify(value) : String(value))}</td></tr>`).join("")}</tbody></table>
      <div class="blockout-palette"></div><h4>Confidence</h4><pre>${pretty(blockout.confidence)}</pre>
      <ul class="blockout-assumptions">${blockout.assumptions.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>
      <p class="blockout-next-view">Suggested next view: ${esc(blockout.next_view)}</p>
      <fieldset id="${id}-regenerate" ${job.match.submitted ? "disabled" : ""}><legend>Regenerate blockout (${job.generate.regenerations}/8)</legend>
        <div class="forge-fields"><label>Height hint (m)<input class="text-control blockout-height" type="number" min="1" max="300" step="any" value="${esc(job.generate.height_hint)}"></label>
        <label>Floor height (m)<input class="text-control blockout-floor" type="number" min="1" max="10" step="any" value="${esc(job.generate.floor_height)}"></label>
        <label>Tower override<select class="select-control blockout-tower"><option value="keep">Keep inferred tower</option><option value="none">No tower</option></select></label></div>
        <div class="blockout-palette-inputs forge-fields"></div>${button("Regenerate", "regenerate", "primary")}</fieldset>`;
    for (const [role, color] of Object.entries(blockout.palette)) {
      const hex = `#${color.hex.replace(/^#/, "")}`;
      const swatch = document.createElement("span"); swatch.textContent = `${role} ${hex} `;
      if (/^#[0-9a-f]{6}$/i.test(hex)) swatch.style.borderLeft = `24px solid ${hex}`;
      $(".blockout-palette", root).append(swatch);
      const label = document.createElement("label"); label.textContent = `${role} hex`;
      const input = document.createElement("input"); input.type = "text"; input.className = "text-control";
      input.pattern = "#?[0-9a-fA-F]{6}"; input.value = hex; input.dataset.role = role;
      label.append(input); $(".blockout-palette-inputs", root).append(label);
    }
    $(".blockout-tower", root).value = job.generate.tower_override === "none" ? "none" : "keep";
    let index = 0;
    const render = () => {
      const view = blockout.views[index];
      $(".blockout-renders img", root).src = `${forge}/jobs/${job.id}/renders/${encode(view)}.png?generation=${job.generate.regenerations}`;
      $(".blockout-view-name", root).textContent = `${view} (${index + 1}/${blockout.views.length})`;
    };
    render();
    $("[data-action=previous]", root).onclick = () => { index = (index + blockout.views.length - 1) % blockout.views.length; render(); };
    $("[data-action=next]", root).onclick = () => { index = (index + 1) % blockout.views.length; render(); };
    if (job.intent === "iterate_blockout") {
      $("fieldset", root).remove();
      return;
    }
    const regenerate = $("[data-action=regenerate]", root);
    regenerate.disabled = !!job.match.submitted || job.generate.regenerations >= 8;
    regenerate.onclick = guard(async () => {
      for (const input of root.querySelectorAll("input")) if (!input.reportValidity()) return;
      const review = matchReview();
      if (review?.submitting || job.match.submitted) return;
      const fieldset = $("fieldset", root); fieldset.disabled = true;
      if (review) { review.submitting = true; $(".forge-submit", review.root).disabled = true; }
      try {
        if (review) await review.saves;
        const palette_hex = {};
        for (const input of root.querySelectorAll("[data-role]")) {
          if (input.value.replace(/^#/, "").toLowerCase() !== blockout.palette[input.dataset.role].hex.replace(/^#/, "").toLowerCase()) palette_hex[input.dataset.role] = input.value;
        }
        updated(await api(`${forge}/jobs/${job.id}/blockout/regenerate`, json({
          height_hint: Number($(".blockout-height", root).value), floor_height: Number($(".blockout-floor", root).value),
          tower_override: $(".blockout-tower", root).value, palette_hex,
        })));
        toast("Regenerating blockout. Waiting for new renders and matches.");
      } finally {
        fieldset.disabled = !!job.match.submitted;
        if (review) { review.submitting = false; $(".forge-submit", review.root).disabled = false; }
      }
    });
  }
}

class MatchReview {
  constructor(root, job, updated) {
    this.root = root; this.job = job; this.updated = updated;
    this.decisions = new Map(job.match.decisions.map((p) => [p.panel_id, p]));
    this.saves = Promise.resolve(); this.saveError = null; this.sheets = [];
    root.innerHTML = `<h3>Match review</h3><p class="microcopy">Auto = blue · Accept = green · Repin = amber · Reject = red. Select a rectangle or panel below.</p><div class="forge-review-grid"><div class="forge-sheets"></div><div class="forge-panel-pane">Select a panel.</div></div><div class="forge-missing"></div><label class="forge-check"><input class="missing-ack" type="checkbox"> Bake with missing views</label><p class="forge-save-state" role="status">Draft not changed</p><button type="button" class="button primary forge-submit">Submit review</button>`;
    this.selected = job.match.panels[0];
    this.loadSheets().catch((error) => toast(error.message, true));
    this.drawSelected(); this.coverage();
    $(".forge-submit", root).onclick = guard(() => this.submit());
  }

  decision(panel) {
    return this.decisions.get(panel.panel_id) || { panel_id: panel.panel_id, decision: panel.decision || "reject", view: panel.auto_view || panel.view || null, iou: panel.iou ?? null };
  }

  missing() {
    const covered = new Set(this.job.inputs?.parent_views_inherited || []);
    for (const panel of this.job.match.panels) { const p = this.decision(panel); if (p.decision !== "reject") covered.add(p.view); }
    return this.job.canonical_views.filter((view) => !covered.has(view));
  }

  coverage() {
    const missing = this.missing();
    $(".forge-missing", this.root).textContent = missing.length ? `Views missing: ${missing.join(", ")}` : "All canonical views covered.";
    $(".missing-ack", this.root).closest("label").hidden = !missing.length;
  }

  async loadSheets() {
    const sources = [
      ...(this.job.inputs?.parent_uploads || []).map((index) => ({ index, filename: `Parent photo ${index + 1}`, job_id: this.job.parent_job, route: `uploads/${index}`, key: "parent_upload_index" })),
      ...this.job.uploads.map((upload) => ({ ...upload, route: `uploads/${upload.index}`, key: "upload_index" })),
      ...(this.job.generate?.segment_refs || []).map((ref, index) => ({ index, filename: `Capture cutout ${index + 1}`, route: `cutouts/${index}.png`, key: "cutout_index" })),
    ];
    for (const upload of sources) {
      const frame = document.createElement("section");
      frame.innerHTML = `<h4>${esc(upload.filename)}</h4><canvas aria-label="Panel overlay for ${esc(upload.filename)}"></canvas><div class="forge-panel-list"></div>`;
      $(".forge-sheets", this.root).append(frame);
      const canvas = $("canvas", frame); const image = new Image();
      const panels = this.job.match.panels.filter((p) => p[upload.key] === upload.index);
      const sheet = { canvas, image, panels }; this.sheets.push(sheet);
      image.onload = () => { canvas.width = image.naturalWidth; canvas.height = image.naturalHeight; this.drawSheets(); };
      image.onerror = () => { toast(`Could not load ${upload.filename}`, true); };
      image.src = `${forge}/jobs/${upload.job_id || this.job.id}/${upload.route}`;
      canvas.onclick = (event) => {
        const bounds = canvas.getBoundingClientRect();
        const x = (event.clientX - bounds.left) * canvas.width / bounds.width;
        const y = (event.clientY - bounds.top) * canvas.height / bounds.height;
        const panel = panels.find((p) => x >= p.bbox[0] && y >= p.bbox[1] && x <= p.bbox[2] && y <= p.bbox[3]);
        if (panel) { this.selected = panel; this.drawSelected(); this.drawSheets(); }
      };
      for (const panel of panels) {
        const select = document.createElement("button"); select.type = "button"; select.className = "button ghost"; select.textContent = panel.panel_id;
        select.onclick = () => { this.selected = panel; this.drawSelected(); this.drawSheets(); };
        $(".forge-panel-list", frame).append(select);
      }
    }
  }

  drawSheets() {
    for (const { canvas, image, panels } of this.sheets) {
      if (!image.complete || !image.naturalWidth) continue;
      const ctx = canvas.getContext("2d"); ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.drawImage(image, 0, 0);
      const scale = canvas.width / Math.max(canvas.clientWidth, 1);
      for (const panel of panels) {
        const decision = this.decisions.get(panel.panel_id)?.decision || "auto";
        const color = { auto: "#62a8ff", accept: "#7be39a", repin: "#ffce69", reject: "#ff727c" }[decision];
        const [x, y, right, bottom] = panel.bbox;
        ctx.fillStyle = `${color}25`; ctx.fillRect(x, y, right - x, bottom - y);
        ctx.strokeStyle = color; ctx.lineWidth = (this.selected === panel ? 4 : 2) * scale;
        ctx.strokeRect(x, y, right - x, bottom - y);
        ctx.font = `${12 * scale}px sans-serif`; ctx.fillStyle = color; ctx.fillText(panel.panel_id, x + 3 * scale, y + 15 * scale);
      }
    }
  }

  drawSelected() {
    const p = this.selected; if (!p) { $(".forge-panel-pane", this.root).textContent = "No replacement panels; inherited views carry over."; return; }
    const decision = this.decision(p);
    const pane = $(".forge-panel-pane", this.root);
    pane.innerHTML = `<h4>${esc(p.panel_id)}</h4><img class="forge-crop" src="${forge}/jobs/${this.job.id}/panels/${encode(p.panel_id)}" alt="Selected panel crop"><p>Auto view: ${esc(p.auto_view || "none")} · IoU: ${esc(String(p.iou ?? "n/a"))} · margin: ${esc(String(p.margin ?? "n/a"))}</p><pre>${pretty(p.scores || p)}</pre><p>Decision: ${esc(this.decisions.has(p.panel_id) ? decision.decision : "auto")}</p><label>Canonical view<select class="select-control">${this.job.canonical_views.map((view) => `<option value="${esc(view)}">${esc(view)}</option>`).join("")}</select></label><div class="button-row">${button("Accept", "accept")}${button("Repin", "repin")}${button("Reject", "reject")}</div>`;
    $("select", pane).value = decision.view || this.job.canonical_views[0];
    pane.onclick = (event) => {
      const action = event.target.closest("[data-action]")?.dataset.action; if (!action || this.submitting) return;
      const view = action === "accept" ? p.auto_view : action === "repin" ? $("select", pane).value : null;
      if (action === "accept" && !view) { toast("No automatic match. Choose a view and Repin.", true); return; }
      this.decisions.set(p.panel_id, { panel_id: p.panel_id, decision: action, view, iou: p.iou ?? null });
      $(".missing-ack", this.root).checked = false;
      this.drawSelected(); this.drawSheets(); this.coverage(); this.save();
    };
  }

  payload(mode) { return { mode, panels: this.job.match.panels.map((p) => this.decision(p)), views_missing: this.missing() }; }

  save() {
    const body = this.payload("draft");
    $(".forge-save-state", this.root).textContent = "Saving draft…";
    this.saves = this.saves.then(async () => {
      try {
        await api(`${forge}/jobs/${this.job.id}/review`, json(body));
        this.saveError = null; $(".forge-save-state", this.root).textContent = "Draft saved";
      } catch (error) { this.saveError = error; $(".forge-save-state", this.root).textContent = `Draft not saved: ${error.message}`; }
    });
  }

  async submit() {
    if (this.submitting) return;
    if (this.missing().length && !$(".missing-ack", this.root).checked) throw new Error("Acknowledge baking with missing views before submitting.");
    if (!window.confirm("Submit these decisions and stage the views? Approve the bake after staging completes.")) return;
    this.submitting = true; $(".forge-submit", this.root).disabled = true;
    try {
      await this.saves;
      const job = await api(`${forge}/jobs/${this.job.id}/review`, json(this.payload("submit")));
      this.updated(job); toast("Review submitted. Waiting for staging.");
    } finally { this.submitting = false; $(".forge-submit", this.root).disabled = false; }
  }
}

class LibraryView {
  constructor(root) {
    this.root = root; this.request = 0;
    root.innerHTML = `<div class="forge-heading"><div><p class="eyebrow">Baked versions and lineage</p><h1>Library</h1></div></div><div class="forge-fields forge-surface"><label>Search<input id="libraryQuery" class="text-control" type="search" placeholder="Asset, variant, note…"></label><label>Origin<select id="libraryOrigin" class="select-control"><option value="">All origins</option><option>bake</option><option>trellis</option></select></label><label class="forge-check"><input id="libraryAccepted" type="checkbox"> Accepted only</label>${button("Refresh", "refresh")}</div><div id="libraryGrid"></div>`;
    let debounce;
    $("#libraryQuery").oninput = () => { clearTimeout(debounce); debounce = setTimeout(guard(() => this.refresh()), 250); };
    for (const id of ["#libraryOrigin", "#libraryAccepted"]) $(id).onchange = guard(() => this.refresh());
    $("[data-action=refresh]", root).onclick = guard(() => this.refresh());
  }

  async refresh() {
    const request = ++this.request;
    const query = new URLSearchParams();
    if ($("#libraryQuery").value) query.set("q", $("#libraryQuery").value);
    if ($("#libraryOrigin").value) query.set("origin", $("#libraryOrigin").value);
    if ($("#libraryAccepted").checked) query.set("accepted", "true");
    const versions = await api(`${forge}/library?${query}`); if (request !== this.request) return;
    const grid = $("#libraryGrid"); grid.replaceChildren();
    if (!versions.length) { grid.textContent = "No versions match these filters."; return; }
    const groups = new Map();
    for (const version of versions) {
      const key = `${version.asset} / ${version.variant}`;
      if (!groups.has(key)) groups.set(key, []); groups.get(key).push(version);
    }
    for (const [name, versions] of groups) {
      const section = document.createElement("section"); section.className = "forge-library-group";
      section.innerHTML = `<h2>${esc(name)}</h2><div class="forge-version-grid"></div>`; grid.append(section);
      for (const version of versions.reverse()) {
        const card = document.createElement("button"); card.type = "button"; card.className = "forge-version-card";
        const frame = version.artifacts?.find((name) => name.startsWith("turntable/") && name.endsWith(".png")) || (version.metrics.turntable_frames ? "turntable/tt_00.png" : null);
        card.innerHTML = `${frame ? `<img src="${artifactPath(version, frame)}" alt="First turntable frame" loading="lazy">` : '<div class="forge-no-thumb">No turntable</div>'}<strong>${version.accepted ? "★ " : ""}v${version.number}</strong><span class="forge-badge">${esc(version.origin)}</span><span class="forge-lineage">${lineage(version)}</span><small>${esc(version.state || "ready")}</small>`;
        card.innerHTML += criticBadge(version.critic?.status);
        card.onclick = guard(() => detail.open(version)); $(".forge-version-grid", section).append(card);
      }
    }
  }
}

class VersionDetail {
  constructor(root) {
    this.root = root; this.loading = Promise.resolve(); this.request = 0;
    root.innerHTML = `<div class="forge-heading"><h2 class="detail-title"></h2>${button("Close detail", "close")}</div><div class="detail-actions button-row"></div><div class="forge-detail-grid"><div><div class="mesh-viewer forge-viewer"></div><p class="detail-viewer-status" role="status"></p><div class="detail-layers"></div></div><div><div class="detail-filmstrip"></div><nav class="forge-subtabs" aria-label="Version detail tabs">${["Bake report", "Views", "History", "Critic"].map((name) => button(name, name)).join("")}</nav><div class="detail-content"></div></div></div><div class="detail-blockout-edit"></div><form class="detail-note" hidden><label>Author<input name="author" class="text-control" value="owner" required maxlength="128"></label><label>Note<textarea name="text" class="text-control" required maxlength="10000"></textarea></label><button class="button primary">Save note</button></form><dialog class="forge-lightbox"><button type="button" class="button secondary">Close</button><img alt="Turntable frame"></dialog>`;
    $("[data-action=close]", root).onclick = () => { root.hidden = true; ++this.request; };
    $(".forge-lightbox button", root).onclick = () => $("dialog", root).close();
    $(".forge-subtabs", root).onclick = (event) => { const name = event.target.closest("[data-action]")?.dataset.action; if (name) this.tab(name); };
    $(".detail-note", root).onsubmit = guard(async (event) => {
      event.preventDefault();
      const form = event.currentTarget; const submit = $("button", form); submit.disabled = true;
      try {
        const version = this.version;
        await api(`${versionPath(version)}/notes`, json(Object.fromEntries(new FormData(form))));
        $("textarea", form).value = ""; form.hidden = true; await this.open(version); this.tab("History"); toast("Note saved.");
      } finally { submit.disabled = false; }
    });
  }

  editBlockout() {
    const version = this.version;
    const blockout = this.job.generate?.blockout;
    if (!version.artifacts.includes("blockout/spec.yaml") || !blockout) throw new Error("Parent blockout is unavailable.");
    const form = document.createElement("form");
    form.innerHTML = `<h3>Edit blockout</h3><p>Only changed values are applied. All views will be matched and reviewed again.</p>
      <div class="forge-fields">
        <label>Height (m)<input name="height" class="text-control" type="number" min="1" max="300" step="any" required></label>
        <label>Floor height (m)<input name="floor_height" class="text-control" type="number" min="1" max="10" step="any" required></label>
        <label>Plinth floors<input name="plinth_floors" class="text-control" type="number" min="1" max="40" step="1" required></label>
        <label class="forge-check"><input name="tower_enabled" type="checkbox">Tower enabled</label>
        <label>Tower width (m)<input name="tower_width" class="text-control" type="number" min="0.01" max="300" step="any" required></label>
        <label>Tower location<select name="tower_location" class="select-control"><option value="rear_center">Rear center</option><option value="front_center">Front center</option><option value="center">Center</option></select></label>
      </div><div class="edit-palette forge-fields"></div><p class="edit-error" role="alert"></p>
      <button class="button primary" type="submit">Create edited blockout</button> ${button("Cancel", "cancel-edit")}`;
    const field = (name) => $(`[name=${name}]`, form);
    const initial = { height: blockout.params.height, floor_height: blockout.params.plinth.floor_height,
      plinth_floors: blockout.params.plinth.floors };
    for (const [key, value] of Object.entries(initial)) field(key).value = value;
    const tower = blockout.params.tower;
    field("tower_enabled").checked = !!tower;
    field("tower_width").value = tower?.width ?? blockout.params.footprint.width / 2;
    field("tower_location").value = tower?.location ?? "rear_center";
    const toggle = () => { for (const key of ["tower_width", "tower_location"]) field(key).disabled = !field("tower_enabled").checked; };
    field("tower_enabled").onchange = toggle; toggle();
    for (const [role, color] of Object.entries(blockout.palette)) {
      const label = document.createElement("label"); label.textContent = `${role} hex `;
      const input = document.createElement("input"); input.className = "text-control"; input.required = true;
      input.pattern = "[0-9a-fA-F]{6}"; input.value = color.hex.replace(/^#/, ""); input.dataset.role = role;
      const swatch = document.createElement("span"); swatch.textContent = "Preview";
      const preview = () => { swatch.style.borderLeft = /^[0-9a-fA-F]{6}$/.test(input.value) ? `24px solid #${input.value}` : ""; };
      input.oninput = preview; preview(); label.append(input, swatch); $(".edit-palette", form).append(label);
    }
    $("[data-action=cancel-edit]", form).onclick = () => form.remove();
    form.onsubmit = async (event) => {
      event.preventDefault();
      const submit = $("button[type=submit]", form); submit.disabled = true;
      $(".edit-error", form).textContent = "";
      try {
        if (!form.reportValidity()) return;
        const edit = {};
        for (const [key, value] of Object.entries(initial)) if (Number(field(key).value) !== value) edit[key] = Number(field(key).value);
        const enabled = field("tower_enabled").checked;
        if (enabled !== !!tower || (enabled && (Number(field("tower_width").value) !== tower.width || field("tower_location").value !== tower.location))) {
          edit.tower = enabled ? { enabled, width: Number(field("tower_width").value), location: field("tower_location").value } : { enabled };
        }
        const palette = {};
        for (const input of form.querySelectorAll("[data-role]")) {
          if (input.value.toLowerCase() !== blockout.palette[input.dataset.role].hex.replace(/^#/, "").toLowerCase()) palette[input.dataset.role] = input.value;
        }
        if (Object.keys(palette).length) edit.palette = palette;
        if (!Object.keys(edit).length) throw new Error("Change at least one value.");
        const body = new FormData();
        body.append("asset", version.asset); body.append("variant", version.variant);
        body.append("intent", "iterate_blockout"); body.append("parent_job", version.job_id);
        body.append("parent_version", version.number); body.append("edit", JSON.stringify(edit));
        body.append("params", JSON.stringify(this.job.params));
        const job = await api(`${forge}/jobs`, { method: "POST", body });
        form.remove(); showTab("forge"); board.card(job);
        board.jobs.get(job.id).root.scrollIntoView({ behavior: "smooth" });
        toast("Blockout edit created. Waiting for new renders and matches.");
      } catch (error) { $(".edit-error", form).textContent = error.message; }
      finally { submit.disabled = false; }
    };
    $(".detail-blockout-edit", this.root).replaceChildren(form);
    field("height").focus();
  }

  async open(summary) {
    const request = ++this.request;
    const version = await api(versionPath(summary));
    const [job, report, harmonize] = await Promise.all([
      version.job_id == null ? {} : api(`${forge}/jobs/${version.job_id}`).catch((error) => ({ history_error: error.message })),
      version.artifacts.includes("bake_report.json") ? api(artifactPath(version, "bake_report.json")) : {},
      version.artifacts.includes("harmonize_report.json") ? api(artifactPath(version, "harmonize_report.json")) : {},
    ]);
    const chain = [version]; let ancestor = version;
    const seen = new Set([version.number]);
    while (ancestor.lineage?.parent_version && !seen.has(ancestor.lineage.parent_version)) {
      ancestor = await api(versionPath({ ...version, number: ancestor.lineage.parent_version }));
      chain.push(ancestor); seen.add(ancestor.number);
    }
    if (request !== this.request) return;
    $(".detail-blockout-edit", this.root).replaceChildren();
    this.version = version; this.job = job; this.report = report; this.harmonize = harmonize; this.chain = chain;
    this.root.hidden = false;
    $(".detail-title", this.root).textContent = `${version.asset} / ${version.variant} · ${lineage(version)}`;
    $(".detail-actions", this.root).innerHTML = button(version.accepted ? "★ Accepted · unstar" : "☆ Accept", "accept") + (version.origin === "trellis" ? "" : button("Iterate", "iterate", "accent")) + (version.artifacts.includes("blockout/spec.yaml") ? button("Edit blockout", "edit-blockout", "accent") : "") + button("Add note", "note");
    $(".detail-actions", this.root).onclick = guard(async (event) => {
      const action = event.target.closest("[data-action]")?.dataset.action;
      if (action === "accept") { await api(`${versionPath(version)}/accept`, json({ accepted: !version.accepted })); await this.open(version); await library.refresh(); }
      if (action === "iterate") await board.iterate(version);
      if (action === "edit-blockout") this.editBlockout();
      if (action === "note") { $(".detail-note", this.root).hidden = false; $("textarea", this.root).focus(); }
    });
    const layers = $(".detail-layers", this.root);
    layers.innerHTML = Object.entries(version.metrics.part_layers || {}).map(([layer, count]) => `<label class="forge-check"><input type="checkbox" checked data-layer="${esc(layer)}"> ${esc(layer)} (${esc(String(count))})</label>`).join("");
    layers.onchange = () => this.applyLayers();
    const filmstrip = $(".detail-filmstrip", this.root); filmstrip.replaceChildren();
    for (const name of version.artifacts.filter((name) => /^turntable\/.*\.png$/.test(name)).sort()) {
      const frame = document.createElement("button"); frame.className = "forge-frame"; frame.type = "button";
      frame.innerHTML = `<img src="${artifactPath(version, name)}" alt="${esc(name)}">`;
      frame.onclick = () => { $("dialog img", this.root).src = artifactPath(version, name); $("dialog", this.root).showModal(); }; filmstrip.append(frame);
    }
    if (!filmstrip.childElementCount) filmstrip.textContent = "No turntable frames.";
    this.tab("Bake report"); this.root.scrollIntoView({ behavior: "smooth" });
    $(".detail-viewer-status", this.root).textContent = "Loading 3D preview…";
    // One reusable viewer, with serialized loads so an older GLB cannot replace a newer selection.
    this.loading = this.loading.catch(() => {}).then(async () => {
      if (request !== this.request) return;
      await loadViewerModules();
      if (!this.viewer) this.viewer = new MeshViewer($(".forge-viewer", this.root));
      this.viewer.disposeObject();
      const glb = version.artifacts.find((name) => name.endsWith(".glb"));
      if (!glb) throw new Error("No GLB artifact available.");
      await this.viewer.load(artifactPath(version, glb), "glb");
      if (request === this.request) { this.applyLayers(); $(".detail-viewer-status", this.root).textContent = "Drag to orbit · scroll to zoom"; }
    }).catch((error) => { if (request === this.request) $(".detail-viewer-status", this.root).textContent = `Preview unavailable: ${error.message}`; });
  }

  applyLayers() {
    if (!this.viewer?.object) return;
    const enabled = new Map([...this.root.querySelectorAll("[data-layer]")].map((input) => [input.dataset.layer, input.checked]));
    const mapping = this.version.part_layer_map || {};
    this.viewer.object.traverse((node) => {
      // GLTFLoader can put primitives below the named part node (multi-material GLBs).
      let part = node;
      while (part && !Object.hasOwn(mapping, part.name)) part = part.parent;
      if (node.isMesh && part) node.visible = enabled.get(mapping[part.name]) !== false;
    });
  }

  tab(name) {
    this.activeTab = name;
    clearTimeout(this.criticTimer);
    for (const tab of this.root.querySelectorAll(".forge-subtabs button")) tab.setAttribute("aria-pressed", String(tab.dataset.action === name));
    const content = $(".detail-content", this.root); const v = this.version;
    if (name === "Critic") {
      content.textContent = "Loading critic…";
      this.showCritic().catch((error) => {
        if (this.activeTab === "Critic" && this.version === v) content.textContent = `Critic unavailable: ${error.message}`;
      });
      return;
    }
    if (name === "Bake report") {
      content.innerHTML = `<dl class="forge-metrics">${Object.entries(v.metrics).map(([key, value]) => `<div><dt>${esc(key.replaceAll("_", " "))}</dt><dd>${typeof value === "object" ? `<pre>${pretty(value)}</pre>` : esc(String(value))}</dd></div>`).join("")}</dl><details><summary>Full bake report JSON</summary><pre>${pretty(this.report)}</pre></details>`;
    } else if (name === "Views") {
      const validation = v.metrics.selfcheck?.views || this.report.view_validation || {};
      const drift = v.metrics.harmonize_drift || {};
      const views = [...new Set([...(this.job.canonical_views || []), ...Object.keys(validation), ...Object.keys(drift)])];
      content.innerHTML = `<table><thead><tr><th>View</th><th>Validation IoU</th><th>Harmonize drift</th></tr></thead><tbody>${views.map((view) => `<tr><td>${esc(view)}</td><td>${esc(JSON.stringify(validation[view] ?? "unavailable"))}</td><td>${esc(String(drift[view] ?? "unavailable"))}</td></tr>`).join("")}</tbody></table><details><summary>Harmonize report</summary><pre>${pretty(this.harmonize)}</pre></details>`;
    } else if (name === "History") {
      if (v.job_id == null) {
        content.innerHTML = `<p>Created: ${esc(v.created_at)}</p><h4>Generation run</h4><pre>${pretty(v.inputs.run || {})}</pre><h4>Notes</h4>${v.notes.map((note) => `<blockquote><p>${esc(note.text)}</p><small>${esc(note.author)} · ${esc(note.at)}</small></blockquote>`).join("") || "<p>No notes yet.</p>"}`;
        return;
      }
      content.innerHTML = `<p class="forge-lineage">${this.chain.map((v) => `v${v.number}`).join(" ← ")} · root v${v.lineage.root_version}</p><p>Created: ${esc(v.created_at)}<br>Job updated: ${esc(this.job.updated_at || "unavailable")}</p>${this.job.history_error ? `<p class="forge-error">Job history unavailable: ${esc(this.job.history_error)}</p>` : ""}<h4>Notes</h4>${[...(this.job.notes || []), ...v.notes].map((note) => `<blockquote><p>${esc(note.text)}</p><small>${esc(note.author)} · ${esc(note.at)}</small></blockquote>`).join("") || "<p>No notes yet.</p>"}<h4>Job decisions and inputs</h4><pre>${pretty({ intent: this.job.intent, decisions: this.job.match?.decisions, inputs: v.inputs })}</pre><h4>Lineage dates</h4><pre>${pretty(this.chain.map((v) => ({ version: v.number, job: v.job_id, created_at: v.created_at })))}</pre>`;
    }
  }

  async showCritic() {
    const version = this.version; const request = this.request;
    const verdict = await api(`${versionPath(version)}/critic`);
    if (request !== this.request || this.activeTab !== "Critic" || this.root.hidden) return;
    const content = $(".detail-content", this.root);
    content.innerHTML = `<div class="button-row">${criticBadge(verdict.status)}<strong>${verdict.score == null ? "No score" : `${esc(verdict.score)} / 100`}</strong>${button("Rerun critic", "rerun-critic")}</div>
      <p>${esc(verdict.summary || "Waiting for the local critic worker.")}</p>
      ${verdict.excerpt ? `<pre class="critic-excerpt">${esc(verdict.excerpt)}</pre>` : ""}
      <ul class="critic-issues">${(verdict.issues || []).map((issue) => `<li><span class="forge-badge">${issue.frame == null ? "All frames" : `Frame ${esc(issue.frame)}`}</span> <strong>${esc(issue.severity)}</strong> · ${esc(issue.kind)}<p>${esc(issue.note)}</p></li>`).join("")}</ul>
      <footer><small>${esc(verdict.model || "Model pending")} · ${esc(verdict.at || "Not run yet")}</small></footer>`;
    $("[data-action=rerun-critic]", content).disabled = version.origin === "trellis" && version.job_id == null;
    $("[data-action=rerun-critic]", content).onclick = guard(async (event) => {
      event.currentTarget.disabled = true;
      try {
        await api(`${versionPath(version)}/critic/rerun`, json({}));
        if (request === this.request && this.activeTab === "Critic") await this.showCritic();
      } finally {
        const rerun = $("[data-action=rerun-critic]", content);
        if (rerun) rerun.disabled = false;
      }
    });
    clearTimeout(this.criticTimer);
    if (verdict.status === "pending") this.criticTimer = setTimeout(() => {
      if (request === this.request && this.activeTab === "Critic" && !this.root.hidden) this.tab("Critic");
    }, 2000);
  }
}

const library = new LibraryView($("#libraryPanel"));
const detail = new VersionDetail($("#versionDetail"));
const board = new BoardView($("#forgePanel"));
// Gen Ladder GL1: the single Capture-to-Library navigation seam.
document.addEventListener("forge:open-library", () => showTab("library"));
for (const tab of document.querySelectorAll("[data-main-tab]")) tab.onclick = () => showTab(tab.dataset.mainTab);
