import { MeshViewer, loadViewerModules } from "./viewer.js";
const $ = (selector) => document.querySelector(selector);
const state = {
  upload: null,
  image: null,
  points: [],
  segment: null,
  mask: null,
  status: null,
  busy: false,
  libraryRunKey: null,
};

const canvas = $("#selectionCanvas");
const context = canvas.getContext("2d");
let meshViewer = null;
let meshViewerReady = null;
let toastTimer = null;


function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast visible${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    let detail = typeof payload === "object" ? payload.detail : payload;
    if (typeof detail !== "string") {
      // FastAPI validation errors arrive as a list of {loc, msg, type} objects
      detail = Array.isArray(detail)
        ? detail.map((issue) => `${(issue.loc || []).join(".")}: ${issue.msg}`).join(" · ")
        : JSON.stringify(detail);
    }
    throw new Error(detail || `Request failed with HTTP ${response.status}`);
  }
  return payload;
}

function setBusy(busy, label = "Reading your selection") {
  state.busy = busy;
  $("#stageBusy").classList.toggle("hidden", !busy);
  $("#stageBusy strong").textContent = label;
  updateControls();
}

function updateControls() {
  const hasImage = Boolean(state.upload && state.image);
  const hasPositive = state.points.some((point) => point.label === 1);
  $("#undoPoint").disabled = state.busy || !state.points.length;
  $("#clearPoints").disabled = state.busy || !state.points.length;
  $("#extractButton").disabled = state.busy || !hasImage || !hasPositive;
  $("#generateButton").disabled = state.busy || !state.segment;
  $("#selectionHint").textContent = !hasImage
    ? "Load an image first."
    : hasPositive
      ? state.points.length > 32
        ? `${state.points.length} prompt points — SAM2 accepts at most 32.`
        : `${state.points.length} prompt point${state.points.length === 1 ? "" : "s"} ready.`
      : "Add one green point to begin.";
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("The browser could not load the image artifact."));
    image.src = `${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`;
  });
}

function drawCanvas() {
  if (!state.image) return;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(state.image, 0, 0, canvas.width, canvas.height);

  if (state.mask) {
    const overlay = document.createElement("canvas");
    overlay.width = canvas.width;
    overlay.height = canvas.height;
    const overlayContext = overlay.getContext("2d");
    overlayContext.drawImage(state.mask, 0, 0, canvas.width, canvas.height);
    const pixels = overlayContext.getImageData(0, 0, canvas.width, canvas.height);
    for (let offset = 0; offset < pixels.data.length; offset += 4) {
      const maskStrength = pixels.data[offset] / 255;
      pixels.data[offset] = 183;
      pixels.data[offset + 1] = 241;
      pixels.data[offset + 2] = 116;
      pixels.data[offset + 3] = Math.round(maskStrength * 132);
    }
    overlayContext.putImageData(pixels, 0, 0);
    context.drawImage(overlay, 0, 0);
  }

  const scale = Math.max(1, canvas.width / Math.max(canvas.clientWidth || canvas.width, 1));
  for (const point of state.points) {
    const radius = 8 * scale;
    context.beginPath();
    context.arc(point.x, point.y, radius, 0, Math.PI * 2);
    context.fillStyle = point.label === 1 ? "#b7f174" : "#ff6f76";
    context.fill();
    context.lineWidth = 2.5 * scale;
    context.strokeStyle = "rgba(8, 10, 9, .92)";
    context.stroke();
    context.beginPath();
    context.arc(point.x, point.y, radius + 4 * scale, 0, Math.PI * 2);
    context.lineWidth = 1.3 * scale;
    context.strokeStyle = point.label === 1 ? "rgba(183,241,116,.72)" : "rgba(255,111,118,.72)";
    context.stroke();
  }
}

function resetSelection() {
  configureLibrarySave(null);
  state.points = [];
  state.segment = null;
  state.mask = null;
  $("#cutoutPreview").classList.remove("ready");
  $("#cutoutImage").removeAttribute("src");
  $("#maskMetrics").classList.add("hidden");
  $("#fallbackNotice").classList.add("hidden");
  $("#resultSection").classList.add("hidden");
  drawCanvas();
  updateControls();
}

async function uploadFile(file) {
  if (!file || !file.type.startsWith("image/")) {
    toast("Choose a PNG, JPEG, or WebP image.", true);
    return;
  }
  setBusy(true, "Uploading image");
  try {
    const body = new FormData();
    body.append("file", file);
    const upload = await api("/v1/ui/uploads", { method: "POST", body });
    const image = await loadImage(upload.image_url);
    state.upload = upload;
    state.image = image;
    canvas.width = upload.width;
    canvas.height = upload.height;
    $("#imageStage").classList.remove("empty");
    $("#imageStage").classList.add("loaded");
    $("#fileDetails").classList.remove("empty");
    $("#fileDetails").innerHTML = `<strong>${escapeHtml(upload.original_name)}</strong><span>${upload.width} × ${upload.height} px · ${upload.sha256.slice(0, 10)}</span>`;
    $("#canvasLabel").textContent = upload.original_name;
    $("#canvasMeta").textContent = `${upload.width} × ${upload.height} · click to include`;
    resetSelection();
    drawCanvas();
    toast("Image ready. Click the object you want to extract.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function canvasPoint(event) {
  const bounds = canvas.getBoundingClientRect();
  return {
    x: Math.min(canvas.width - 1, Math.max(0, (event.clientX - bounds.left) * canvas.width / bounds.width)),
    y: Math.min(canvas.height - 1, Math.max(0, (event.clientY - bounds.top) * canvas.height / bounds.height)),
    label: event.shiftKey || event.button === 2 ? 0 : 1,
  };
}

const SAM2_MAX_POINTS = 32;

async function extractSelection() {
  if (!state.upload || !state.points.some((point) => point.label === 1)) return;
  if (state.points.length > SAM2_MAX_POINTS) {
    toast(`SAM2 accepts at most ${SAM2_MAX_POINTS} prompt points (${state.points.length} placed). Undo or Clear a few — a handful on one object is normally plenty.`, true);
    return;
  }
  setBusy(true, "Reading your selection");
  try {
    const segment = await api(`/v1/ui/uploads/${state.upload.image_id}/segments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points: state.points }),
    });
    state.segment = segment;
    state.mask = await loadImage(segment.mask_url);
    drawCanvas();
    $("#cutoutImage").src = `${segment.cutout_url}?v=${Date.now()}`;
    $("#cutoutPreview").classList.add("ready");
    $("#maskMetrics").classList.remove("hidden");
    $("#engineMetric").textContent = segment.engine === "sam2" ? "SAM2" : "Fallback";
    $("#coverageMetric").textContent = `${(segment.coverage * 100).toFixed(1)}%`;
    $("#confidenceMetric").textContent = segment.score == null ? "n/a" : segment.score.toFixed(3);
    const fallback = $("#fallbackNotice");
    if (segment.fallback_reason) {
      fallback.textContent = "SAM2 was unavailable for this request, so the deterministic color-region fallback produced this mask. Review it carefully.";
      fallback.classList.remove("hidden");
    } else {
      fallback.classList.add("hidden");
    }
    updateControls();
    toast(`${segment.engine === "sam2" ? "SAM2" : "Fallback"} mask ready.`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function updateGenerationMode() {
  const mode = $("#generationMode").value;
  const pipeline = mode.startsWith("pipeline");
  $("#silhouetteControls").classList.toggle("hidden", pipeline);
  $("#promptField").classList.toggle("hidden", !pipeline);
  $("#generationPrompt").classList.toggle("hidden", !pipeline);
  $("#generationBoundary").textContent = mode === "silhouette"
    ? "Creates a textured 2.5D OBJ immediately."
    : mode === "pipeline-mock"
      ? "Exercises the complete pipeline with clearly marked mock geometry."
      : "Produces full inferred geometry; requires the dedicated 24 GB GPU lane.";
}

async function generateMesh() {
  if (!state.segment || !state.upload) return;
  if (restorePoll !== null) window.clearInterval(restorePoll);
  const mode = $("#generationMode").value;
  const payload = mode === "silhouette"
    ? {
        generation: "silhouette",
        grid_size: Number($("#detailInput").value),
        depth: Number($("#depthInput").value),
      }
    : {
        generation: "pipeline",
        prompt: $("#generationPrompt").value.trim() || null,
        provider: mode === "pipeline-real" ? "trellis2" : null,
        mock: mode !== "pipeline-real",
        mode: "hero",
      };

  setBusy(
    true,
    mode === "silhouette"
      ? "Building preview mesh"
      : mode === "pipeline-real"
        ? "Entering the GPU queue"
        : "Generating 3D asset",
  );
  let queuePoll = null;
  if (mode === "pipeline-real") {
    const updateQueueLabel = async () => {
      try {
        const runtime = await api("/v1/ui/status");
        const waiting = runtime.generation.real_pipeline_queue_depth;
        $("#stageBusy strong").textContent = waiting > 0
          ? `GPU queue · ${waiting} job${waiting === 1 ? "" : "s"} waiting`
          : "TRELLIS.2 running on the RTX 3090";
      } catch (_error) {
        // Keep the request's current progress label if a status poll is interrupted.
      }
    };
    void updateQueueLabel();
    queuePoll = window.setInterval(updateQueueLabel, 2500);
  }
  try {
    const result = await api(
      `/v1/ui/uploads/${state.upload.image_id}/segments/${state.segment.segment_id}/generate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    if (result.status !== "success" || !result.primary_asset_url) {
      const errors = result.manifest?.items?.[0]?.errors?.join(" · ") || "Generation did not return a mesh.";
      throw new Error(errors);
    }
    await renderPipelineResult(result);
    toast("Mesh ready to inspect and download.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (queuePoll !== null) window.clearInterval(queuePoll);
    setBusy(false);
    void loadStatus();
  }
}

function showFailedResult(record) {
  configureLibrarySave(null);
  const errors = record.error
    || record.response?.manifest?.items?.[0]?.errors?.join(" · ")
    || "Generation did not return a mesh.";
  $("#resultSection").classList.remove("hidden");
  $("#meshViewer").innerHTML = `<div class="viewer-loading">Run finished without a mesh.<br /><small>${escapeHtml(String(errors).slice(0, 300))}</small></div>`;
}

async function renderPipelineResult(result) {
  if (!result.primary_asset_url) {
    // item-level failure (e.g. provider OOM): never hand the viewer a null URL
    showFailedResult({ response: result });
    return;
  }
  $("#resultSection").classList.remove("hidden");
  configureLibrarySave(result);
  $("#downloadMesh").href = result.primary_asset_url;
  setOptionalDownload("#downloadMaterial", result.material_url);
  setOptionalDownload("#downloadTexture", result.texture_url);
  const boundary = result.generation?.quality_boundary;
  $("#resultSummary").textContent = boundary
    ? `${boundary}. ${result.generation.cell_count.toLocaleString()} occupied cells, ${result.generation.face_count.toLocaleString()} faces.`
    : `Pipeline run ${result.run_id || "completed"} produced a ${result.asset_type?.toUpperCase() || "3D"} asset.`;
  if (result.sprite_sheet_url) {
    $("#spriteSheet").src = result.sprite_sheet_url;
    $("#spriteResult").classList.remove("hidden");
  } else {
    $("#spriteResult").classList.add("hidden");
  }
  const viewer = meshViewer || await meshViewerReady;
  if (viewer) {
    try {
      await viewer.load(result.primary_asset_url, result.asset_type, result.material_url);
    } catch (viewerError) {
      $("#meshViewer").innerHTML = `<div class="viewer-loading">Mesh created, but the browser preview could not open it.<br /><small>${escapeHtml(viewerError.message)}</small></div>`;
    }
  }
  $("#resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
}

// Gen Ladder GL1: imports use server-side files, keyed to the displayed run.
function configureLibrarySave(result) {
  state.libraryRunKey = result?.status === "success" && result.primary_asset_url ? result.record_key : null;
  $("#librarySaveForm").classList.toggle("hidden", !state.libraryRunKey);
  $("#libraryAsset").value = (state.upload?.original_name || "trellis-asset").toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "-").replace(/\.{2,}/g, "-").replace(/^[^a-z0-9]+/, "").slice(0, 128) || "trellis-asset";
  $("#libraryVariant").value = "default";
  $("#saveToLibrary").disabled = false;
  $("#openSavedLibrary").classList.add("hidden");
}

$("#librarySaveForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const recordKey = state.libraryRunKey;
  if (!recordKey || $("#saveToLibrary").disabled) return;
  $("#saveToLibrary").disabled = true;
  try {
    const version = await api(`/v1/ui/runs/${encodeURIComponent(recordKey)}/library`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset: $("#libraryAsset").value, variant: $("#libraryVariant").value }),
    });
    toast(`Saved to Library · ${version.asset} / ${version.variant} · v${version.number}`);
    if (state.libraryRunKey === recordKey) $("#openSavedLibrary").classList.remove("hidden");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (state.libraryRunKey === recordKey) $("#saveToLibrary").disabled = false;
  }
});
$("#openSavedLibrary").addEventListener("click", () => document.dispatchEvent(new Event("forge:open-library")));

// --- refresh persistence: restore the latest generation run on page load.
// The backend records every pipeline run under the (bind-mounted) UI store;
// a refreshed page picks up the newest record, restores the source image and
// cutout, re-renders a finished result, or polls until a running one lands.
let restorePoll = null;

async function restoreLatestRun() {
  let record;
  try {
    const listing = await api("/v1/ui/runs?limit=1");
    record = listing.runs?.[0];
  } catch (_error) {
    return;
  }
  if (!record) return;
  if (record.image_id) {
    try {
      const image = await loadImage(`/v1/ui/uploads/${record.image_id}/source`);
      state.upload = { image_id: record.image_id, width: image.width, height: image.height };
      state.image = image;
      canvas.width = image.width;
      canvas.height = image.height;
      $("#imageStage").classList.remove("empty");
      $("#imageStage").classList.add("loaded");
      $("#fileDetails").classList.remove("empty");
      $("#fileDetails").innerHTML = `<strong>Restored session</strong><span>${image.width} × ${image.height} px · run ${escapeHtml(record.record_key)}</span>`;
      drawCanvas();
    } catch (_error) {
      // image restore is best-effort; the run record still works without it
    }
  }
  if (record.image_id && record.segment_id) {
    $("#cutoutImage").src = `/v1/ui/uploads/${record.image_id}/segments/${record.segment_id}/cutout?v=${Date.now()}`;
    $("#cutoutPreview").classList.add("ready");
  }
  if (record.status === "completed" && record.response) {
    await renderPipelineResult(record.response);
    return;
  }
  if (record.status === "failed") {
    toast(record.error || "Generation failed.", true);
    return;
  }
  if (record.status !== "running") return;
  setBusy(true, "Restoring run · TRELLIS.2 still generating");
  const poll = async () => {
    try {
      const current = await api(`/v1/ui/runs/${record.record_key}`);
      if (current.status === "completed" && current.response) {
        window.clearInterval(restorePoll);
        restorePoll = null;
        setBusy(false);
        await renderPipelineResult(current.response);
        toast("Generation finished — result restored.");
      } else if (current.status === "failed") {
        window.clearInterval(restorePoll);
        restorePoll = null;
        setBusy(false);
        toast(current.error || "Generation failed.", true);
      }
    } catch (_error) {
      // transient poll failure: keep the interval alive
    }
  };
  restorePoll = window.setInterval(poll, 10000);
  void poll();
}

function setOptionalDownload(selector, url) {
  const element = $(selector);
  if (url) {
    element.href = url;
    element.classList.remove("hidden");
  } else {
    element.classList.add("hidden");
  }
}

async function loadStatus() {
  try {
    const status = await api("/v1/ui/status");
    state.status = status;
    $("#apiState").className = "status-pill ready";
    $("#apiState").innerHTML = "<i></i>API online";
    const segmentation = status.segmentation;
    $("#segmenterState").textContent = segmentation.engine === "sam2"
      ? `SAM2 · ${segmentation.loaded ? "loaded" : "ready"} · ${segmentation.device}`
      : "Fallback segmenter · CPU";
    const realOption = $("#generationMode option[value='pipeline-real']");
    realOption.disabled = !status.generation.real_pipeline_allowed;
    if (!status.generation.real_pipeline_allowed) {
      realOption.textContent = "TRELLIS.2 · GPU lane currently reserved";
    } else if (status.generation.real_pipeline_busy) {
      const waiting = status.generation.real_pipeline_queue_depth;
      realOption.textContent = waiting > 0
        ? `TRELLIS.2 · ${waiting} waiting · joins queue`
        : "TRELLIS.2 · running · joins queue";
    } else {
      realOption.textContent = "TRELLIS.2 · full AI mesh";
    }
  } catch (error) {
    $("#apiState").className = "status-pill error";
    $("#apiState").innerHTML = "<i></i>API unavailable";
    toast(error.message, true);
  }
}


meshViewerReady = loadViewerModules()
  .then(() => {
    meshViewer = new MeshViewer($("#meshViewer"));
    return meshViewer;
  })
  .catch((error) => {
    $("#meshViewer").innerHTML = `<div class="viewer-loading">3D preview is unavailable in this browser.<br /><small>${escapeHtml(error.message)}</small></div>`;
    return null;
  });

canvas.addEventListener("pointerdown", (event) => {
  if (!state.image || state.busy) return;
  event.preventDefault();
  state.points.push(canvasPoint(event));
  state.segment = null;
  state.mask = null;
  $("#cutoutPreview").classList.remove("ready");
  $("#maskMetrics").classList.add("hidden");
  drawCanvas();
  updateControls();
});
canvas.addEventListener("contextmenu", (event) => event.preventDefault());

$("#imageInput").addEventListener("change", (event) => uploadFile(event.target.files?.[0]));
const dropZone = $("#dropZone");
for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
}
dropZone.addEventListener("drop", (event) => uploadFile(event.dataTransfer?.files?.[0]));
$("#undoPoint").addEventListener("click", () => {
  state.points.pop();
  state.segment = null;
  state.mask = null;
  drawCanvas();
  updateControls();
});
$("#clearPoints").addEventListener("click", resetSelection);
$("#extractButton").addEventListener("click", extractSelection);
$("#generationMode").addEventListener("change", updateGenerationMode);
$("#generateButton").addEventListener("click", generateMesh);
$("#depthInput").addEventListener("input", (event) => { $("#depthValue").value = event.target.value; });
$("#detailInput").addEventListener("input", (event) => { $("#detailValue").value = event.target.value; });

updateGenerationMode();
updateControls();
loadStatus();
void restoreLatestRun();

export { api, toast, escapeHtml };
