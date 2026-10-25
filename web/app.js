const API = {
  health: "/api/health",
  library: "/api/library",
  upload: "/api/upload",
  proposals: "/api/proposals",
  jobs: "/api/jobs",
  style: "/api/style",
  driveConfig: "/api/drive/config",
  driveFolders: "/api/drive/folders",
  driveList: "/api/drive/list",
  driveStatus: "/api/drive/status",
  driveOauthStart: "/api/drive/oauth/start",
  driveScan: "/api/drive/scan",
  driveSync: "/api/drive/sync",
  driveTestFile: "/api/drive/test-file",
  driveWriteTest: "/api/drive/test-write",
  driveRetryUpload: "/api/drive/retry-upload",
};

const VIDEO_NICHE_LABELS = {
  beauty_skincare: "Beauty & Skincare",
  fashion: "Fashion & Style",
  lifestyle_travel: "Lifestyle & Travel",
  fitness_sport: "Fitness & Sport",
  ugc_ecommerce: "UGC & Productos",
  generic_viral: "Genérico / Viral",
};

// Presentational filter for the six known test uploads. The API, SQLite rows,
// media files, and future uploads remain untouched.
const HIDDEN_TEST_UPLOAD_IDS = new Set([
  "074e33f3a6004af388135a2e339480c1",
  "21f0c12763ef425e921e286cd0d12dd8",
  "2a4f73637d2a4edf86d0e2c91b6ea0c9",
  "d96ccf90a12e45e79def4ddb6c8e217a",
  "e263a3e2c522487f9e0cad5f99b7250b",
  "f8c270f8b2a74f4a88f13990f82af564",
]);

function isHiddenTestUploadId(id) {
  return HIDDEN_TEST_UPLOAD_IDS.has(String(id ?? ""));
}

function getVisibleLibraryItems(items) {
  return Array.isArray(items) ? items.filter((item) => !isHiddenTestUploadId(item?.id)) : [];
}

const state = {
  library: [],
  libraryLoaded: false,
  selectedId: null,
  health: null,
  style: null,
  activeJob: null,
  pollTimer: null,
  editProposal: null,
  editOptionsSourceId: null,
  styleLoaded: false,
  drive: null,
  driveStatus: null,
  driveToken: null,
  driveTokenExpiresAt: 0,
  googleTokenClient: null,
  uploadingVideo: false,
  activePage: "home",
};

let csrfToken = "";

const $ = (selector) => document.querySelector(selector);
const byId = (id) => document.getElementById(id);

function setActivePage(page) {
  const allowedPages = new Set(["home", "create", "library", "settings"]);
  const nextPage = allowedPages.has(page) ? page : "home";
  state.activePage = nextPage;
  document.querySelectorAll("[data-page-section]").forEach((section) => {
    section.hidden = section.dataset.pageSection !== nextPage;
  });
  document.querySelectorAll(".primary-nav [data-page], .mobile-nav [data-page]").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === nextPage);
    button.setAttribute("aria-current", button.dataset.page === nextPage ? "page" : "false");
  });
}

async function requestJson(url, options = {}) {
  let response;
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  try {
    response = await fetch(url, { ...options, method, headers, credentials: "same-origin" });
  } catch (error) {
    throw new Error(`No se pudo conectar con la aplicación. ${error.message}`);
  }
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("La sesión ha caducado. Inicia sesión de nuevo.");
  }
  const text = await response.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = { detail: text }; }
  }
  if (!response.ok) {
    const message = body?.detail || body?.error || body?.message || `No se pudo completar la solicitud (código ${response.status}).`;
    if (response.status === 403 && String(url).startsWith("/api/drive/")) {
      state.driveToken = null;
      state.driveTokenExpiresAt = 0;
    }
    const error = new Error(String(message));
    error.status = response.status;
    throw error;
  }
  return body;
}

async function loadHealth() {
  return requestJson(API.health);
}

async function loadLibrary() {
  const result = await requestJson(API.library);
  return Array.isArray(result) ? result : (result?.items || result?.library || []);
}

async function uploadVideo(file, niche, styleNotes, editingStyle) {
  return requestJson(API.upload, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Upload-Filename": encodeURIComponent(file.name),
      "X-Video-Niche": niche,
      "X-Editing-Style": editingStyle,
      "X-Style-Notes": encodeURIComponent(styleNotes),
    },
    body: file,
  });
}

async function prepareEditProposal(
  sourceId,
  subtitleMode,
  outputFps,
  outputProfile,
  outputQuality,
  creativePlan,
  renderBackend,
  renderMode = "full",
  customization = {},
) {
  return requestJson(API.proposals, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_id: sourceId,
      subtitle_mode: subtitleMode,
      output_fps: Number(outputFps),
      output_profile: outputProfile,
      output_quality: outputQuality,
      creative_plan: creativePlan,
      render_backend: renderBackend,
      render_mode: renderMode,
      customization,
    }),
  });
}

function collectPanelCustomization() {
  return {
    caption_color: byId("custom-caption-color").value,
    caption_timing: byId("custom-caption-timing").value,
    caption_size: byId("custom-caption-size").value,
    caption_position: byId("custom-caption-position").value,
    auto_zoom: byId("custom-autozoom").value,
    transitions: byId("custom-transitions").value,
    audio_volume: Number(byId("custom-original-volume").value),
    noise_reduction: byId("custom-noise-reduction").checked,
    music_enabled: byId("custom-music-enabled").checked,
    music_ducking: byId("custom-music-ducking").checked,
    titles: byId("custom-layer-titles").checked,
    accents: byId("custom-layer-accents").checked,
    image_filter: byId("custom-image-filter").value,
    notes: byId("custom-free-instructions").value,
  };
}

function invalidateEditOptions() {
  clearEditProposal();
  byId("cost-value").textContent = "Pendiente de propuesta";
  byId("cost-note").textContent = "Prepara una propuesta nueva para estos ajustes.";
  updateProcessButton();
}

function addPlanField(row, labelText, fieldName, type, value, options = []) {
  const label = document.createElement("label");
  label.className = "timeline-field";
  label.append(document.createTextNode(labelText));
  let field;
  if (type === "select") {
    field = document.createElement("select");
    for (const [optionValue, optionLabel] of options) {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = optionLabel;
      field.append(option);
    }
  } else {
    field = document.createElement("input");
    field.type = type;
    if (type === "number") {
      field.min = "0";
      field.step = "0.1";
      field.placeholder = "s";
      field.required = true;
    }
    if (type === "color") field.value = value || "#FFD84D";
  }
  field.dataset.field = fieldName;
  if (type !== "color" && value !== undefined && value !== "") field.value = value;
  label.append(field);
  row.append(label);
  return field;
}

function addCreativePlanRow(kind, initial = {}) {
  const config = {
    cut: { id: "edit-cuts-list", className: "cut-row" },
    zoom: { id: "edit-zooms-list", className: "zoom-row" },
    sticker: { id: "edit-stickers-list", className: "sticker-row" },
  }[kind];
  if (!config) return;
  const list = byId(config.id);
  if (list.querySelectorAll(".timeline-row").length >= 12) {
    displayToast("Se permiten hasta 12 elementos de cada tipo.", true);
    return;
  }
  const row = document.createElement("div");
  row.className = `timeline-row ${config.className}`;
  if (kind === "cut") {
    addPlanField(row, "Desde (s)", "start_seconds", "number", initial.start_seconds);
    addPlanField(row, "Hasta (s)", "end_seconds", "number", initial.end_seconds);
  } else if (kind === "zoom") {
    addPlanField(row, "Desde (s)", "start_seconds", "number", initial.start_seconds);
    addPlanField(row, "Hasta (s)", "end_seconds", "number", initial.end_seconds);
    addPlanField(row, "Efecto", "effect", "select", initial.effect || "zoomIn", [
      ["zoomIn", "Acercar"],
      ["zoomOut", "Alejar"],
    ]);
  } else {
    addPlanField(row, "Sticker", "sticker", "select", initial.sticker || "sparkle", [
      ["sparkle", "Destello"],
      ["star", "Estrella"],
      ["heart", "Corazón"],
      ["bolt", "Rayo"],
    ]);
    addPlanField(row, "Inicio (s)", "start_seconds", "number", initial.start_seconds);
    addPlanField(row, "Duración (s)", "duration_seconds", "number", initial.duration_seconds);
    addPlanField(row, "Posición", "position", "select", initial.position || "topRight", [
      ["topLeft", "Arriba izquierda"],
      ["topRight", "Arriba derecha"],
      ["bottomLeft", "Abajo izquierda"],
      ["bottomRight", "Abajo derecha"],
      ["center", "Centro"],
    ]);
    addPlanField(row, "Animación", "effect", "select", initial.effect || "zoomIn", [
      ["zoomIn", "Entrada con zoom"],
      ["zoomOut", "Salida con zoom"],
    ]);
    addPlanField(row, "Color", "color", "color", initial.color || "#FFD84D");
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-timeline-row";
  remove.textContent = "Eliminar";
  remove.setAttribute("aria-label", `Eliminar ${kind === "cut" ? "corte" : kind === "zoom" ? "zoom" : "sticker"}`);
  remove.addEventListener("click", () => {
    row.remove();
    invalidateEditOptions();
  });
  row.append(remove);
  for (const field of row.querySelectorAll("input, select")) {
    field.addEventListener("input", invalidateEditOptions);
    field.addEventListener("change", invalidateEditOptions);
  }
  list.append(row);
  invalidateEditOptions();
}

function collectCreativePlan() {
  const readRows = (id, fields) => Array.from(byId(id).querySelectorAll(".timeline-row")).map((row, index) => {
    const item = {};
    for (const fieldName of fields) {
      const field = row.querySelector(`[data-field="${fieldName}"]`);
      if (!field || (field.type === "number" && field.value.trim() === "")) {
        throw new Error(`Completa los tiempos del elemento ${index + 1}.`);
      }
      item[fieldName] = field.type === "number" ? Number(field.value) : field.value;
    }
    return item;
  });
  return {
    cuts: readRows("edit-cuts-list", ["start_seconds", "end_seconds"]),
    zooms: readRows("edit-zooms-list", ["start_seconds", "end_seconds", "effect"]),
    stickers: readRows("edit-stickers-list", [
      "sticker", "start_seconds", "duration_seconds", "position", "effect", "color",
    ]),
  };
}

async function startProcessing(proposal, unknownCostAcknowledged) {
  return requestJson(API.jobs, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      proposal_id: proposal.proposal_id,
      unknown_cost_acknowledged: unknownCostAcknowledged,
      authorize_paid_apis: byId("paid-consent").checked,
      authorize_modal_compute: proposal.api_plan?.renderer_backend === "modal",
      authorize_openai_transcription: proposal.api_plan?.openai_transcription === true,
      authorize_gemini_analysis: proposal.api_plan?.gemini_video_analysis === true,
    }),
  });
}

async function postDrive(url, payload = {}) {
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, access_token: await getDriveAccessToken() }),
  });
}
async function loadJob(jobId) {
  return requestJson(`${API.jobs}/${encodeURIComponent(jobId)}`);
}

async function loadStyle() {
  return requestJson(API.style);
}

async function loadDriveStatus() {
  return requestJson(API.driveStatus);
}

async function startDriveOAuth() {
  return requestJson(API.driveOauthStart, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

async function scanDrive() {
  return requestJson(API.driveScan, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

async function testDriveWrite() {
  return requestJson(API.driveWriteTest, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
}

async function retryDriveUpload(sourceId) {
  return requestJson(API.driveRetryUpload, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_id: sourceId }),
  });
}

async function saveStyle(style) {
  return requestJson(API.style, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(style),
  });
}

function sourceMediaUrl(id) {
  return `/media/incoming/${encodeURIComponent(id)}`;
}

function outputMediaUrl(id) {
  return `/media/outputs/${encodeURIComponent(id)}`;
}

function getSelectedItem() {
  return getVisibleLibraryItems(state.library)
    .find((item) => String(item.id) === String(state.selectedId)) || null;
}

function ensureVisibleSelection() {
  const visibleItems = getVisibleLibraryItems(state.library);
  if (!visibleItems.some((item) => String(item.id) === String(state.selectedId))) {
    state.selectedId = visibleItems[0]?.id ?? null;
  }
}

function libraryRefreshMessage() {
  const visibleCount = getVisibleLibraryItems(state.library).length;
  const hiddenCount = state.library.length - visibleCount;
  if (visibleCount) return "Estado y biblioteca actualizados.";
  if (hiddenCount) return `La biblioteca está vacía en esta vista; ${hiddenCount} pruebas se conservan ocultas.`;
  return "Tu biblioteca está vacía. Añade un vídeo para empezar.";
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return "No disponible";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "No disponible";
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}:${String(secs).padStart(2, "0")} min`;
}

function formatDate(value) {
  if (value === null || value === undefined || value === "") return "Fecha no disponible";
  const numeric = Number(value);
  const date = Number.isFinite(numeric) ? new Date(numeric * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "Fecha no disponible";
  return new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short", year: "numeric" }).format(date);
}

function formatCost(value) {
  if (value === null || value === undefined || value === "") return "Desconocido";
  const amount = Number(value);
  return Number.isFinite(amount) ? `$${amount.toFixed(4)} USD` : "Desconocido";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function whiteLabelText(value) {
  return String(value ?? "")
    .replace(/\bCloudflare\s*R2\b/giu, "almacenamiento privado")
    .replace(/\bCloudflare\b/giu, "almacenamiento privado")
    .replace(/\bR2\b/giu, "almacenamiento privado")
    .replace(/\bModal\b/giu, "motor MATRIX")
    .replace(/\bReplit\b/giu, "espacio de trabajo")
    .replace(/\bFFmpeg\b/giu, "procesamiento de vídeo");
}

function displayToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = whiteLabelText(message);
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(displayToast.timer);
  displayToast.timer = setTimeout(() => { toast.hidden = true; }, 5200);
}

function statusLabel(status) {
  const labels = {
    queued: "En espera", pending: "Pendiente", ready: "Listo para procesar", validating: "Comprobando",
    processing: "En curso", transcribing: "Transcribiendo el audio", rendering: "Generando el vídeo",
    downloading: "Descargando el resultado", completed: "Completado", complete: "Completado",
    failed: "Error", error: "Error", rejected: "No admitido",
    normalizing: "Preparando el formato del vídeo", transcribing_with_whisper: "Transcribiendo el audio",
    preparing_captions: "Preparando subtítulos",
    analyzing_with_gemini: "Analizando el vídeo",
    uploading: "Preparando el vídeo",
    uploading_captions: "Preparando subtítulos",
    uploading_to_modal: "Preparando el procesamiento",
    rendering_on_modal: "Generando el vídeo",
    downloading_from_modal: "Preparando el resultado",
    uploading_to_r2: "Guardando el resultado privado",
    validating_output: "Comprobando el resultado", interrupted: "Interrumpido",
  };
  return labels[String(status || "").toLowerCase()] || (status ? String(status) : "No disponible");
}

function renderLibrary() {
  const list = byId("library-list");
  const visibleItems = getVisibleLibraryItems(state.library);
  byId("library-count").textContent = visibleItems.length
    ? `${visibleItems.length} ARCHIVO${visibleItems.length === 1 ? "" : "S"}`
    : "0 ARCHIVOS";
  renderSummary();
  renderRecentVideos();
  if (!visibleItems.length) {
    const emptyMessage = state.libraryLoaded
      ? "Todavía no hay vídeos. Añade un archivo MP4 o MOV para empezar."
      : "Añade tu primer vídeo para empezar a crear contenido.";
    list.innerHTML = `<div class="empty-state"><div class="empty-glyph">▧</div><h3>Tu biblioteca está vacía</h3><p>${escapeHtml(emptyMessage)}</p><button class="outline-button" type="button" data-action="upload">Añadir un vídeo</button></div>`;
    return;
  }
  list.innerHTML = visibleItems.map((item) => {
    const name = item.original_filename || item.filename || `Archivo ${item.id}`;
    const status = String(item.status || "").toLowerCase();
    const statusClass = ["failed", "error", "rejected"].includes(status) ? "status-error" : ["completed", "complete"].includes(status) ? "status-complete" : "";
    const thumbnail = item.thumbnail_url
      ? `<img src="${escapeHtml(item.thumbnail_url)}" alt="" loading="lazy">`
      : `<span class="library-thumb-fallback">${escapeHtml(item.format || "VIDEO")}</span>`;
    return `<button type="button" class="library-item ${String(item.id) === String(state.selectedId) ? "selected" : ""}" data-id="${escapeHtml(item.id)}">
      <span class="library-thumb" aria-hidden="true">${thumbnail}</span>
      <span class="library-item-body">
        <span class="library-item-top"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><span class="file-type">${escapeHtml(item.source_label || item.format || "VIDEO")}</span></span>
        <span class="library-item-meta"><span>${formatDuration(item.duration_seconds)}</span><span>${formatBytes(item.size_bytes)}</span><span>${escapeHtml(formatDate(item.created_at))}</span></span>
      </span>
      <span class="library-item-status ${statusClass}">${escapeHtml(statusLabel(item.status))}</span>
    </button>`;
  }).join("");
}

function renderRecentVideos() {
  const list = byId("recent-video-list");
  if (!list) return;
  const recent = getVisibleLibraryItems(state.library)
    .slice()
    .sort((a, b) => {
      const timestamp = (item) => {
        const numeric = Number(item.created_at);
        return Number.isFinite(numeric) ? numeric : (Date.parse(item.created_at || "") || 0) / 1000;
      };
      return timestamp(b) - timestamp(a);
    })
    .slice(0, 3);
  if (!recent.length) {
    list.innerHTML = `<div class="recent-empty">Tus vídeos recientes aparecerán aquí cuando subas un archivo.</div>`;
    return;
  }
  list.innerHTML = recent.map((item) => {
    const filename = item.original_filename || item.filename || `Vídeo ${item.id}`;
    return `<button class="recent-video-item" type="button" data-recent-id="${escapeHtml(item.id)}">
      <span class="recent-video-icon" aria-hidden="true">▶</span>
      <span class="recent-video-copy"><strong>${escapeHtml(filename)}</strong><small>${escapeHtml(statusLabel(item.status))} · ${formatDuration(item.duration_seconds)}</small></span>
      <span class="recent-video-arrow" aria-hidden="true">→</span>
    </button>`;
  }).join("");
}

function renderSummary() {
  if (!state.libraryLoaded) return;
  const visibleItems = getVisibleLibraryItems(state.library);
  const pendingStatuses = new Set(["ready", "pending", "queued", "processing", "transcribing", "transcribing_with_whisper", "rendering", "downloading", "normalizing", "preparing_captions", "analyzing_with_gemini", "uploading", "uploading_captions", "uploading_to_modal", "rendering_on_modal", "downloading_from_modal", "uploading_to_r2", "validating_output"]);
  const processedStatuses = new Set(["completed", "complete"]);
  const errorStatuses = new Set(["failed", "error", "rejected", "interrupted"]);
  const count = (statuses) => visibleItems.filter((item) => statuses.has(String(item.status || "").toLowerCase())).length;
  byId("summary-pending").textContent = String(count(pendingStatuses));
  byId("summary-processed").textContent = String(count(processedStatuses));
  byId("summary-errors").textContent = String(count(errorStatuses));
  byId("summary-pending-note").textContent = "Listos o en curso";
  byId("summary-source").textContent = "Datos actualizados";
}

function clearEditProposal() {
  state.editProposal = null;
  const summary = byId("proposal-summary");
  if (summary) {
    summary.hidden = true;
    summary.replaceChildren();
  }
  const approval = byId("render-approval");
  if (approval) approval.hidden = true;
  if (byId("paid-consent")) byId("paid-consent").checked = false;
  if (byId("unknown-cost-consent")) byId("unknown-cost-consent").checked = false;
}

function resetCreativePlanControls() {
  for (const id of ["edit-cuts-list", "edit-zooms-list", "edit-stickers-list"]) {
    const list = byId(id);
    if (list) list.replaceChildren();
  }
}

function syncEditOptionsForSelectedItem(item) {
  if (!item || state.editOptionsSourceId === item.id) return;
  state.editOptionsSourceId = item.id;
  const editingStyle = item.editing_style === "natural_premium" ? "natural_premium" : "dynamic";
  byId("upload-editing-style").value = editingStyle;
  document.querySelectorAll("[data-editing-style]").forEach((button) => {
    const selected = button.dataset.editingStyle === editingStyle;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  byId("edit-subtitle-mode").value = editingStyle === "natural_premium" ? "none" : "dynamic";
  const is4kSource = Number(item.width) >= 2160 && Number(item.height) >= 3840;
  byId("edit-output-profile").value = is4kSource ? "2160p" : "1080p";
  byId("edit-output-quality").value = "high";
  const supportedFps = [25, 30, 50, 60];
  const sourceFps = Math.round(Number(item.fps) || 30);
  const selectedFps = supportedFps.includes(sourceFps)
    ? sourceFps
    : supportedFps.reduce((best, value) =>
      Math.abs(value - sourceFps) < Math.abs(best - sourceFps) ? value : best,
    );
  byId("edit-output-fps").value = String(selectedFps);
  resetCreativePlanControls();
  syncRenderBackendControls();
  clearEditProposal();
}

function syncRenderBackendControls() {
  const modal = byId("edit-render-backend").value === "modal";
  const fps = byId("edit-output-fps");
  const profile = byId("edit-output-profile");
  const selectedItem = getSelectedItem();
  const is4kSource = Number(selectedItem?.width) >= 2160 && Number(selectedItem?.height) >= 3840;
  for (const option of fps.options) {
    option.disabled = Number(option.value) > 60 || ![25, 30, 50, 60].includes(Number(option.value));
  }
  for (const option of profile.options) {
    option.disabled = option.value === "1440p" || (option.value === "2160p" && !is4kSource);
  }
  byId("edit-output-quality").disabled = modal;
  const simpleSubtitles = byId("edit-subtitle-mode").querySelector('option[value="simple"]');
  if (simpleSubtitles) simpleSubtitles.disabled = modal;
  byId("add-zoom-button").disabled = modal;
  for (const field of byId("edit-zooms-list").querySelectorAll("input, select")) {
    field.disabled = modal;
  }
  if (modal) {
    if (!fps.value || Number(fps.value) > 60) fps.value = "30";
    if (profile.selectedOptions[0]?.disabled) profile.value = is4kSource ? "2160p" : "1080p";
    if (byId("edit-subtitle-mode").value === "simple") byId("edit-subtitle-mode").value = "dynamic";
    byId("render-backend-note").textContent =
      "Motor MATRIX: salida vertical hasta 2160 × 3840 y 60 FPS para fuentes compatibles. El coste de procesamiento no está cotizado aquí. Admite cortes, subtítulos dinámicos y stickers; los zooms manuales y el logo global no se aplican.";
  }
  updateCreationSummary();
}

function renderEditProposal(proposal) {
  state.editProposal = proposal;
  const modeLabels = {
    none: "Sin subtítulos",
    dynamic: "Subtítulos dinámicos con palabras destacadas",
  };
  const source = proposal.source || {};
  const edit = proposal.edit || {};
  const costs = proposal.costs || {};
  const apiPlan = proposal.api_plan || {};
  const captionStyle = edit.caption_style || {};
  const creativePlan = edit.creative_plan || { cuts: [], zooms: [], stickers: [] };
  const duration = Number(source.duration_seconds);
  const outputDuration = Number(edit.expected_output_duration_seconds);
  const sourceBitrate = Number(source.video_bit_rate_bps || source.format_bit_rate_bps || 0);
  const unknownCost = costs.total_estimate_usd === null || costs.total_estimate_usd === undefined;
  const rangeLabel = (item) => `${Number(item.start_seconds).toFixed(2)}–${Number(item.end_seconds).toFixed(2)} s`;
  const cutsLabel = (creativePlan.cuts || []).length
    ? (creativePlan.cuts || []).map(rangeLabel).join(", ")
    : "ninguno";
  const zoomsLabel = (creativePlan.zooms || []).length
    ? (creativePlan.zooms || []).map((item) => `${rangeLabel(item)} (${item.effect === "zoomIn" ? "acercar" : "alejar"})`).join(", ")
    : "ninguno";
  const stickersLabel = (creativePlan.stickers || []).length
    ? (creativePlan.stickers || []).map((item) => `${item.sticker}, ${Number(item.start_seconds).toFixed(2)} s, ${Number(item.duration_seconds).toFixed(2)} s`).join("; ")
    : "ninguno";
  const lines = [
    `Vídeo: ${proposal.source_name || "No disponible"} · ${Number.isFinite(duration) ? `${duration.toFixed(2)} s` : "duración desconocida"}`,
    "Motor: MATRIX Engine",
    `Modo: ${edit.render_mode === "quick_demo" ? "muestra de 5 segundos" : "vídeo completo"}`,
    `Nicho: ${edit.niche_label || VIDEO_NICHE_LABELS[edit.niche] || VIDEO_NICHE_LABELS.generic_viral}`,
    `Subtítulos: fuente ${captionStyle.fontFamily || "Montserrat"} · resaltado ${captionStyle.highlightColor || "#FFC0CB"} · texto ${captionStyle.textColor || "#FFFFFF"}`,
    `Briefing: ${edit.style_notes ? `${edit.style_notes} (las directivas explícitas de tipografía y color se aplican a esta propuesta)` : "ninguna"}`,
    `Origen: ${source.origin || "No identificado"}`,
    `Entrada: ${source.width || "?"} × ${source.height || "?"} · ${source.fps || "?"} FPS · ${source.video_codec || "códec desconocido"}`,
    `Bitrate de entrada medido: ${sourceBitrate > 0 ? `${(sourceBitrate / 1000000).toFixed(2)} Mbps` : "no informado"}`,
    `Salida: MP4 ${edit.output_width || 1080} × ${edit.output_height || 1920} · ${edit.output_fps || "?"} FPS · calidad ${edit.output_quality || "?"}`,
    `Duración final prevista: ${Number.isFinite(outputDuration) ? `${outputDuration.toFixed(2)} s` : "no disponible"}`,
    `Subtítulos: ${modeLabels[edit.subtitle_mode] || "No disponible"}`,
    `Análisis visual: ${apiPlan.gemini_video_analysis ? "previsto tras autorizar" : "no se iniciará"}`,
    `Transcripción del audio: ${apiPlan.openai_transcription ? `marcas por palabra, ${apiPlan.openai_transcription_chunks} fragmento/s` : "no se iniciará"}.`,
    `Audio: ${edit.keep_original_audio ? "se conserva el audio original (y se recorta junto al vídeo)" : "la entrada no tiene audio"}; no se añade música ni normalización`,
    `Logo: ${edit.logo_enabled ? "se usará el logo ya configurado" : "ninguno configurado"}`,
    `Encuadre: ${edit.framing || "no disponible"}`,
    `Cortes que se quitarán: ${cutsLabel}`,
    `Zooms: ${zoomsLabel}`,
    `Stickers con animación de zoom: ${stickersLabel}`,
    "El análisis visual y la transcripción requieren autorización; los cortes se muestran como sugerencias y no se aplican automáticamente.",
    "Destino: almacenamiento privado del proyecto",
    `Transcripción: ${formatCost(costs.openai_usd)}`,
    `Análisis visual: ${formatCost(costs.gemini_usd)} (no cotizado)`,
    `Procesamiento: ${formatCost(costs.modal_compute_usd)} (no cotizado)`,
    `Total estimado: ${formatCost(costs.total_estimate_usd)}`,
  ];
  const warnings = (proposal.warnings || []).filter(Boolean);
  const summary = byId("proposal-summary");
  summary.innerHTML = `<h3>Propuesta de edición</h3><ul>${lines.map((line) => `<li>${escapeHtml(whiteLabelText(line))}</li>`).join("")}</ul>${warnings.length ? `<p>${warnings.map((warning) => escapeHtml(whiteLabelText(warning))).join(" ")}</p>` : ""}<small>La propuesta se preparó sin llamadas externas. Caduca en 15 minutos.</small>`;
  summary.hidden = false;
  byId("cost-value").textContent = formatCost(costs.total_estimate_usd);
  byId("cost-note").textContent = costs.estimate_note || "No hay tarifas verificadas para calcular el coste.";
  byId("unknown-cost-wrap").hidden = !unknownCost;
  byId("unknown-cost-consent").checked = false;
  byId("paid-consent").checked = false;
  byId("paid-consent-copy").textContent =
    "Autorizo el procesamiento del audio para transcripción, el análisis visual del vídeo original y el render del máster; el MP4 final se guardará en almacenamiento privado. El coste puede variar.";
  byId("approve-render-button").querySelector("span").textContent = "Autorizar y crear";
  byId("render-approval").hidden = false;
  updateProcessButton();
}

function renderSelectedItem() {
  const item = getSelectedItem();
  const video = byId("source-video");
  const placeholder = $(".stage-placeholder");
  if (!item) {
    state.editOptionsSourceId = null;
    clearEditProposal();
    video.hidden = true;
    video.removeAttribute("src");
    placeholder.hidden = false;
    hideOutput();
    byId("selected-status").textContent = "NINGÚN VÍDEO";
    byId("file-summary").innerHTML = `<div class="summary-empty">Los datos del vídeo aparecerán aquí al seleccionarlo.</div>`;
    byId("cost-value").textContent = "Desconocido";
    byId("cost-note").textContent = "Selecciona un vídeo para consultar la estimación disponible.";
    byId("unknown-cost-wrap").hidden = true;
    byId("retry-drive-upload-button").hidden = true;
    updateProcessButton();
    updateCreationSummary(null);
    return;
  }
  syncEditOptionsForSelectedItem(item);
  video.src = sourceMediaUrl(item.id);
  video.load();
  video.hidden = false;
  placeholder.hidden = true;
  byId("selected-status").textContent = statusLabel(item.status).toLocaleUpperCase("es");
  const fields = [
    ["NICHO", item.niche_label || VIDEO_NICHE_LABELS[item.niche] || VIDEO_NICHE_LABELS.generic_viral],
    ["NOTAS DE ESTILO", item.style_notes || "Ninguna"],
    ["ORIGEN", item.source_label || "Equipo"],
    ["DURACIÓN", formatDuration(item.duration_seconds)],
    ["TAMAÑO", formatBytes(item.size_bytes)],
    ["RESOLUCIÓN", Number.isFinite(Number(item.width)) && Number.isFinite(Number(item.height)) ? `${item.width} × ${item.height}` : "No disponible"],
    ["VÍDEO", item.video_codec || "No disponible"],
    ["AUDIO", item.audio_codec || "No disponible"],
    ["FPS", item.fps ?? "No disponible"],
    ["ROTACIÓN", item.rotation ?? "No disponible"],
    ["ESTADO", statusLabel(item.status)],
  ];
  byId("file-summary").innerHTML = `<div class="metadata-grid">${fields.map(([key, value]) => `<div class="metadata-cell"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`;
  if (state.editProposal?.source_id !== item.id) {
    const costKnown = item.cost_estimate_usd !== null && item.cost_estimate_usd !== undefined;
    byId("cost-value").textContent = formatCost(item.cost_estimate_usd);
    byId("cost-note").textContent = costKnown
      ? "Estimación calculada para una edición sin subtítulos; revisa la propuesta exacta antes de autorizar."
      : "Prepara una propuesta para ver qué servicios se usarían y qué costes se conocen.";
    byId("unknown-cost-wrap").hidden = true;
  }
  const itemOutput = item.output_id ?? item.output?.id ?? (typeof item.output === "string" ? item.output : null);
  if (itemOutput) showOutput(itemOutput);
  else hideOutput();
  if (item.error) {
    byId("file-summary").insertAdjacentHTML("beforeend", `<div class="job-error">${escapeHtml(item.error)}</div>`);
  }
  if (Array.isArray(item.attempt_history) && item.attempt_history.length) {
    const attemptsMarkup = item.attempt_history.map((attempt) => {
      const when = attempt.created_at ? formatDate(attempt.created_at) : "fecha no disponible";
      const retryLabel = attempt.retry_of_job_id ? " · reintento del intento anterior" : "";
      return `<li><strong>Intento ${escapeHtml(attempt.attempt_number)} · ${escapeHtml(statusLabel(attempt.status))}</strong><span>${escapeHtml(when)}${retryLabel}</span>${attempt.error ? `<p>${escapeHtml(attempt.error)}</p>` : ""}</li>`;
    }).join("");
    byId("file-summary").insertAdjacentHTML(
      "beforeend",
      `<section class="attempt-history" aria-label="Historial de intentos"><strong>Historial de intentos</strong><ol>${attemptsMarkup}</ol></section>`,
    );
  }
  if (item.drive_error) {
    byId("file-summary").insertAdjacentHTML("beforeend", `<div class="job-error">El render local se conserva. Error al guardar en Drive: ${escapeHtml(item.drive_error)}</div>`);
  }
  byId("retry-drive-upload-button").hidden = item.can_retry_drive_upload !== true;
  updateProcessButton();
  updateCreationSummary(item);
}

function updateCreationSummary(item = getSelectedItem()) {
  const name = byId("creation-summary-name");
  const duration = byId("creation-summary-duration");
  const category = byId("creation-summary-category");
  const captions = byId("creation-summary-captions");
  const output = byId("creation-summary-output");
  const status = byId("creation-summary-status");
  const image = byId("creation-summary-image");
  const placeholder = $("#creation-summary-thumbnail .summary-thumbnail-empty");
  if (!item) {
    name.textContent = "Ningún vídeo seleccionado";
    duration.textContent = "Pendiente";
    category.textContent = "Pendiente";
    captions.textContent = "Pendiente de vídeo";
    output.textContent = "Pendiente de vídeo";
    status.textContent = "PENDIENTE DE VÍDEO";
    image.hidden = true;
    image.removeAttribute("src");
    if (placeholder) placeholder.hidden = false;
    byId("create-video-button").disabled = true;
    byId("quick-demo-button").disabled = true;
    return;
  }
  name.textContent = item.original_filename || item.filename || "Vídeo seleccionado";
  duration.textContent = formatDuration(item.duration_seconds);
  category.textContent =
    item.niche_label || VIDEO_NICHE_LABELS[item.niche] || VIDEO_NICHE_LABELS.generic_viral;
  captions.textContent = byId("custom-captions-enabled").checked
    ? `Activados · ${byId("custom-caption-color").selectedOptions[0]?.textContent || "Rosa pastel"}`
    : "Desactivados";
  const outputDimensions = {
    "720p": "720 × 1280",
    "1080p": "1080 × 1920",
    "2160p": "2160 × 3840",
  };
  output.textContent =
    `${outputDimensions[byId("edit-output-profile").value] || "Perfil pendiente"} · ${byId("edit-output-fps").value || "—"} FPS`;
  status.textContent = "VÍDEO SELECCIONADO";
  byId("create-video-button").disabled = false;
  byId("quick-demo-button").disabled = false;
  if (item.thumbnail_url) {
    image.src = item.thumbnail_url;
    image.hidden = false;
    if (placeholder) placeholder.hidden = true;
    return;
  }
  const video = byId("source-video");
  const capture = () => {
    if (!video.videoWidth || !video.videoHeight) return;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 180;
      canvas.height = 320;
      const context = canvas.getContext("2d");
      if (!context) return;
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      image.src = canvas.toDataURL("image/jpeg", 0.78);
      image.hidden = false;
      if (placeholder) placeholder.hidden = true;
    } catch {
      // Thumbnail preview is optional; do not report a fake thumbnail.
    }
  };
  if (video.readyState >= 2) capture();
  else video.addEventListener("loadeddata", capture, { once: true });
}

function renderHealth(health) {
  state.health = health;
  const set = (id, configured) => {
    const el = byId(id);
    el.textContent = configured === true ? "Configurada · sin verificar" : configured === false ? "No configurada" : "No disponible";
    el.className = configured === true ? "health-ok" : configured === false ? "health-no" : "health-unknown";
  };
  set("health-openai", health?.openai_key_configured);
  const modal = health?.modal || {};
  const modalStatus = byId("health-modal");
  modalStatus.textContent = modal.sdk_installed && modal.credentials_configured
    ? "Configuración detectada; prueba remota pendiente"
    : "Configuración pendiente";
  modalStatus.className = modal.sdk_installed && modal.credentials_configured ? "health-ok" : "health-unknown";
  byId("health-environment").textContent = "Hasta 2160 × 3840 · hasta 60 FPS; sin render de prueba";
  const r2 = health?.r2_storage || {};
  const engineTag = byId("engine-summary-tag");
  const engineTitle = byId("engine-summary-title");
  if (engineTag) {
    const configured = modal.sdk_installed && modal.credentials_configured && r2.ready === true;
    engineTag.textContent = configured ? "CONFIGURACIÓN LISTA" : "FALTA CONFIGURACIÓN";
    if (engineTitle) {
      engineTitle.textContent = configured
        ? "Motor MATRIX Engine: Configuración lista"
        : "Motor MATRIX Engine: Configuración pendiente";
    }
    engineTag.setAttribute("aria-label", configured
      ? "Configuración detectada; el despliegue del worker y un render real siguen pendientes."
      : "Falta completar la configuración del motor o el almacenamiento privado.");
  }
  const drive = health?.drive_connected;
  const driveReady = health?.drive_oauth_ready;
  const pickerReady = health?.drive_picker_configured;
  byId("health-drive").textContent = drive === true ? "Conectado" : drive === false ? "No conectado" : "No disponible";
  if (drive !== true && driveReady) byId("health-drive").textContent = "OAuth listo";
  else if (drive !== true && pickerReady) byId("health-drive").textContent = "Picker opcional listo";
  byId("health-drive").className = drive === true || driveReady || pickerReady ? "health-ok" : "health-muted";
  const connected = Boolean(health);
  byId("connection-label").textContent = connected ? "Servicio disponible" : "Sin conexión";
  $(".local-indicator").classList.toggle("connected", connected);
}

function renderDriveStatus(drive) {
  state.driveStatus = drive || null;
  const title = byId("drive-panel-title");
  const detail = byId("drive-status-detail");
  const connect = byId("drive-connect-button");
  const scan = byId("drive-scan-button");
  const writeTest = byId("drive-write-test-button");
  if (!drive) {
    title.textContent = "Estado no disponible";
    detail.textContent = "No se pudo consultar la configuración local de OAuth.";
    connect.disabled = true;
    scan.disabled = true;
    writeTest.disabled = true;
    return;
  }
  if (drive.storage_error) {
    title.textContent = "No se pudo abrir la conexión guardada";
    detail.textContent = "La clave de cifrado no coincide con la guardada. Activa OAuth y vuelve a autorizar Drive; no se han mostrado tokens.";
    connect.textContent = "Reautorizar OAuth";
  } else if (drive.needs_reauthorization) {
    title.textContent = "Se necesitan permisos nuevos";
    detail.textContent = "La conexión guardada no incluye todos los permisos OAuth requeridos. Pulsa «Reautorizar OAuth» para actualizarla desde la cuenta secundaria.";
    connect.textContent = "Reautorizar OAuth";
  } else if (drive.identity_mismatch) {
    title.textContent = "La conexión no corresponde a la cuenta autorizada";
    detail.textContent = "El token guardado no se usará. Vuelve a conectar seleccionando la cuenta secundaria configurada.";
    connect.textContent = "Reautorizar OAuth";
  } else if (drive.authorized && drive.ready !== true) {
    title.textContent = "Conexión guardada · integración desactivada";
    detail.textContent = "El token cifrado se conserva, pero OAuth está desactivado y no se harán llamadas a Drive.";
    connect.textContent = "Conectar Google Drive (OAuth)";
  } else if (drive.verified) {
    title.textContent = "Acceso a Google Drive verificado";
    detail.textContent = "La última consulta de Drive respondió correctamente. Escanea 01_ENTRADA para buscar archivos nuevos.";
    connect.textContent = "Reautorizar OAuth";
  } else if (drive.authorized) {
    title.textContent = "Autorización guardada · sin verificar";
    detail.textContent = "El token cifrado está guardado. Escanea 01_ENTRADA para comprobar permisos y descargar vídeos nuevos.";
    connect.textContent = "Reautorizar OAuth";
  } else if (drive.ready) {
    title.textContent = "Autorización pendiente";
    detail.textContent = "La configuración está lista, pero todavía no se ha conectado ninguna cuenta.";
    connect.textContent = "Conectar Google Drive (OAuth)";
  } else {
    title.textContent = drive.enabled ? "Configuración incompleta" : "Conexión desactivada";
    const missing = Array.isArray(drive.missing_configuration) ? drive.missing_configuration : [];
    detail.textContent = missing.length
      ? `Pendiente de configuración: ${missing.join(", ")}. No se ha iniciado OAuth.`
      : "OAuth está desactivado por seguridad. No se ha contactado con Google.";
    connect.textContent = "Conectar Google Drive (OAuth)";
  }
  const automatic = drive.automatic_scan || {};
  const intervalMinutes = Math.max(1, Math.round(Number(automatic.interval_seconds || 900) / 60));
  if (automatic.enabled) {
    if (automatic.last_error) {
      detail.textContent += " El último escaneo automático informó errores; se volverá a intentar en el siguiente intervalo.";
    } else if (automatic.last_scan_at) {
      const summary = automatic.last_summary || {};
      detail.textContent += ` Último escaneo automático: ${summary.imported || 0} nuevo(s), ${summary.skipped || 0} omitido(s), ${summary.failed || 0} error(es).`;
    } else {
      detail.textContent += ` Escaneo automático cada ${intervalMinutes} min cuando la cuenta secundaria esté autorizada y el servidor activo; no inicia renders.`;
    }
  }
  connect.disabled = drive.ready !== true;
  scan.disabled = drive.authorized !== true || drive.ready !== true;
  writeTest.disabled = !canWriteDriveProbe();
  const healthDrive = byId("health-drive");
  if (drive.verified) {
    healthDrive.textContent = "Acceso verificado";
    healthDrive.className = "health-ok";
  } else if (drive.authorized) {
    healthDrive.textContent = "Autorización guardada · sin verificar";
    healthDrive.className = "health-muted";
  } else {
    healthDrive.textContent = "No conectado";
    healthDrive.className = "health-muted";
  }
}

function canWriteDriveProbe() {
  const drive = state.driveStatus || {};
  return Boolean(
    drive.ready === true
    && drive.authorized === true
    && drive.identity_verified === true
    && drive.needs_reauthorization !== true
  );
}

function renderJob(job, sourceName = "") {
  if (isHiddenTestUploadId(job?.source_id)) {
    state.activeJob = null;
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    byId("job-status-tag").textContent = "RESULTADO OCULTO";
    byId("job-content").textContent = "Los datos y archivos de esta prueba se conservan, pero no se muestran en esta vista.";
    hideOutput();
    updateProcessButton();
    return;
  }
  state.activeJob = job;
  const source = getVisibleLibraryItems(state.library)
    .find((item) => String(item.id) === String(job.source_id));
  if (source) {
    source.status = job.status;
    source.error = job.error;
    source.output_id = job.output_id;
    if (job.estimated_cost_usd !== null && job.estimated_cost_usd !== undefined) {
      source.cost_estimate_usd = job.estimated_cost_usd;
    }
    renderLibrary();
  }
  if (job.estimated_cost_usd !== null && job.estimated_cost_usd !== undefined) {
    byId("cost-value").textContent = formatCost(job.estimated_cost_usd);
    byId("cost-note").textContent = "Estimación informada para este trabajo; el importe final puede variar.";
  }
  const status = String(job.status || "").toLowerCase();
  const progress = job.progress === null || job.progress === undefined ? Number.NaN : Number(job.progress);
  const progressText = Number.isFinite(progress) ? `${Math.max(0, Math.min(100, progress))}%` : "Avance no disponible";
  const progressMarkup = Number.isFinite(progress)
    ? `<div class="job-progress-track" aria-label="${escapeHtml(progressText)}"><div class="job-progress-fill" style="width:${Math.max(0, Math.min(100, progress))}%"></div></div>`
    : `<div class="job-progress-unmeasured">El servicio no informa el porcentaje de avance.</div>`;
  const phase = job.phase ? statusLabel(job.phase) : statusLabel(job.status);
  const metadata = job.output_metadata || {};
  const dimensions = metadata.dimensions;
  const bitrate = Number(metadata.video_bit_rate_bps || metadata.format_bit_rate_bps || 0);
  const outputMetadataMarkup = ["completed", "complete"].includes(status) && dimensions
    ? `<div class="job-output-metadata"><strong>Mediciones reales de FFprobe</strong><span>${escapeHtml(dimensions.width)} × ${escapeHtml(dimensions.height)} · ${escapeHtml(metadata.fps ?? "FPS no disponible")} FPS · ${escapeHtml(metadata.video_codec || "códec no disponible")}</span><span>Bitrate: ${bitrate > 0 ? `${(bitrate / 1000000).toFixed(2)} Mbps` : "no informado por FFprobe"} · preset solicitado: ${escapeHtml(metadata.quality_preset || "no disponible")}</span><span>Duración final: ${Number.isFinite(Number(metadata.duration_seconds)) ? `${Number(metadata.duration_seconds).toFixed(2)} s` : "no disponible"}</span></div>`
    : "";
  const gemini = job.gemini_analysis || {};
  const suggestedCuts = Array.isArray(gemini.suggested_cuts) ? gemini.suggested_cuts : [];
  const moments = Array.isArray(gemini.key_moments) ? gemini.key_moments : [];
  const zooms = Array.isArray(gemini.suggested_zooms) ? gemini.suggested_zooms : [];
  const copySeo = gemini.copy_and_seo || {};
  const transcription = job.transcription || {};
  const geminiMarkup = gemini.model
    ? `<div class="job-output-metadata"><strong>Estrategia Copywriting &amp; SEO</strong><span>Transcripción: ${escapeHtml(transcription.word_count ?? 0)} palabras con marcas temporales. Análisis visual: ${escapeHtml(whiteLabelText(gemini.summary || "Sin resumen disponible"))}</span><span>Archivo temporal eliminado: ${gemini.remote_file_deleted === true ? "sí" : "no confirmado"} · unidades informadas: ${escapeHtml(gemini.usage?.total_tokens ?? "no disponibles")}</span><span>Momentos visuales: ${moments.length}; reencuadres sugeridos ${zooms.length}, aplicados ${gemini.suggested_zooms_applied === true ? "sí" : "no"}. Los cortes no se aplicaron y requieren revisión humana.</span>${moments.length ? `<ul>${moments.map((item) => `<li>${Number(item.start_seconds).toFixed(2)}–${Number(item.end_seconds).toFixed(2)} s · ${escapeHtml(item.label || "")}: ${escapeHtml(whiteLabelText(item.reason || ""))}</li>`).join("")}</ul>` : ""}${suggestedCuts.length ? `<ul>${suggestedCuts.map((cut) => `<li>Corte sugerido ${Number(cut.start_seconds).toFixed(2)}–${Number(cut.end_seconds).toFixed(2)} s · ${escapeHtml(whiteLabelText(cut.reason || ""))}</li>`).join("")}</ul>` : "<span>Sin cortes sugeridos.</span>"}</div>`
    : "";
  const r2 = job.r2_storage || {};
  const r2Markup = r2.object_key
    ? `<div class="job-output-metadata"><strong>Almacenamiento privado</strong><span>${escapeHtml(whiteLabelText(r2.object_key))} · ${Number.isFinite(Number(r2.size_bytes)) ? `${(Number(r2.size_bytes) / 1048576).toFixed(1)} MiB` : "tamaño no disponible"} · verificación ${r2.verified === true ? "confirmada" : "pendiente"}</span></div>`
    : "";
  renderSeoStrategy(copySeo);
  byId("job-status-tag").textContent = statusLabel(job.status).toLocaleUpperCase("es");
  byId("job-content").innerHTML = `<div class="job-live">
    <div><strong>${escapeHtml(sourceName || "Procesamiento")}</strong><p>${escapeHtml(phase)} · Identificador ${escapeHtml(job.id ?? "No disponible")}</p>${progressMarkup}</div>
    <span class="job-progress-label">${escapeHtml(whiteLabelText(progressText))}</span>
    ${job.error ? `<div class="job-error">${escapeHtml(whiteLabelText(job.error))}</div>` : ""}
    ${geminiMarkup}
    ${outputMetadataMarkup}
    ${r2Markup}
  </div>`;
  const outputId = job.output_id || job.output?.id;
  if (outputId) showOutput(outputId);
  const finished = ["completed", "complete", "failed", "error", "rejected"].includes(status);
  if (finished) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    if (status === "completed" || status === "complete") displayToast("El procesamiento ha terminado. El resultado real está disponible abajo.");
  }
  updateProcessButton();
}

function showOutput(outputId) {
  const panel = byId("output-panel");
  const video = byId("output-video");
  const url = outputMediaUrl(outputId);
  video.src = url;
  byId("output-link").href = url;
  byId("output-empty").hidden = true;
  panel.hidden = false;
}

function renderSeoStrategy(copySeo = {}) {
  const container = byId("step5-seo-content");
  const button = byId("copy-seo-button");
  if (!container || !button) return;
  const hook = typeof copySeo.hook === "string" ? copySeo.hook.trim() : "";
  const caption = typeof copySeo.caption === "string" ? copySeo.caption.trim() : "";
  const seoTitle = typeof copySeo.seo_title === "string" ? copySeo.seo_title.trim() : "";
  const hashtags = Array.isArray(copySeo.hashtags)
    ? copySeo.hashtags.filter((tag) => typeof tag === "string" && tag.trim()).map((tag) => tag.trim())
    : [];
  const hasContent = Boolean(hook || caption || seoTitle || hashtags.length);
  button.disabled = !hasContent;
  button.dataset.copySeo = "";
  if (!hasContent) {
    container.innerHTML = '<p class="step5-seo-empty">Hook, caption y hashtags aparecerán aquí cuando exista un análisis real.</p>';
    return;
  }
  const copyText = [
    hook ? `Hook: ${hook}` : "",
    caption ? `Caption: ${caption}` : "",
    seoTitle ? `SEO: ${seoTitle}` : "",
    hashtags.length ? `Hashtags: ${hashtags.join(" ")}` : "",
  ].filter(Boolean).join("\n");
  button.dataset.copySeo = copyText;
  container.innerHTML = `<dl class="step5-seo-values">
    <div><dt>Hook</dt><dd>${hook ? escapeHtml(hook) : "No disponible"}</dd></div>
    <div><dt>Caption</dt><dd>${caption ? escapeHtml(caption) : "No disponible"}</dd></div>
    ${seoTitle ? `<div><dt>Título SEO</dt><dd>${escapeHtml(seoTitle)}</dd></div>` : ""}
    <div><dt>Hashtags</dt><dd>${hashtags.length ? hashtags.map(escapeHtml).join(" ") : "No disponibles"}</dd></div>
  </dl>`;
}

async function copySeoStrategy(button) {
  const text = button.dataset.copySeo || "";
  if (!text) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const field = document.createElement("textarea");
      field.value = text;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      const copied = document.execCommand("copy");
      field.remove();
      if (!copied) throw new Error("No se pudo copiar el texto.");
    }
    const originalLabel = button.textContent;
    button.textContent = "Copiado";
    setTimeout(() => { button.textContent = originalLabel; }, 1800);
  } catch {
    displayToast("No se pudo copiar la estrategia. Selecciona el texto y cópialo manualmente.", true);
  }
}

function hideOutput() {
  const panel = byId("output-panel");
  const video = byId("output-video");
  panel.hidden = true;
  byId("output-empty").hidden = false;
  video.pause();
  video.removeAttribute("src");
  video.load();
  byId("output-link").href = "#";
}

async function initializeMasterPreview() {
  const video = byId("remotion-final-video");
  const source = video?.querySelector("source[data-src]");
  const status = byId("remotion-final-video-status");
  if (!video || !source || !status) return;

  try {
    const sourceUrl = source.dataset.src;
    const response = await fetch(sourceUrl, {
      method: "GET",
      headers: { Range: "bytes=0-0" },
      credentials: "same-origin",
    });
    if (response.body) await response.body.cancel().catch(() => {});
    if (!response.ok) {
      status.textContent = response.status === 404
        ? "El MP4 final aún no se ha generado."
        : `El MP4 final no está disponible (HTTP ${response.status}).`;
      return;
    }
    source.src = sourceUrl;
    video.hidden = false;
    status.hidden = true;
    video.load();
  } catch {
    status.textContent = "No se pudo comprobar la disponibilidad del MP4 final.";
  }
}

function updateProcessButton() {
  const item = getSelectedItem();
  const busy = state.activeJob && !["completed", "complete", "failed", "error", "rejected"].includes(String(state.activeJob.status || "").toLowerCase());
  const itemStatus = String(item?.status || "").toLowerCase();
  const unavailable = ["processing", "queued"].includes(itemStatus)
    || ["completed", "failed"].includes(itemStatus);
  const proposal = state.editProposal;
  const unknownCost = !proposal
    || proposal.costs?.total_estimate_usd === null
    || proposal.costs?.total_estimate_usd === undefined;
  const consentOk = byId("paid-consent").checked
    && (!unknownCost || byId("unknown-cost-consent").checked);
  const needsGemini = proposal?.api_plan?.gemini_video_analysis === true;
  const missingApi = proposal && (
    proposal.api_plan?.renderer_backend !== "modal"
    || !proposal.api_plan?.modal_sdk_installed
    || !proposal.api_plan?.modal_credentials_configured
    || (proposal.api_plan?.openai_transcription && !proposal.api_plan?.openai_key_configured)
    || (needsGemini && !proposal.api_plan?.gemini_key_configured)
  );
  byId("process-button").disabled = !item || Boolean(busy) || unavailable;
  byId("approve-render-button").disabled = !proposal || !consentOk || Boolean(missingApi) || Boolean(busy);
  byId("process-hint").textContent = !item
    ? "Selecciona un vídeo antes de continuar."
    : unavailable
      ? "Este vídeo ya tiene un resultado, está en proceso o requiere revisión de su error."
      : busy
        ? "Hay un trabajo en curso."
        : !proposal
          ? "Prepara una propuesta local; no llamará a proveedores ni consumirá créditos."
          : missingApi
            ? "Falta configurar una API necesaria; no se puede autorizar el render."
            : !byId("paid-consent").checked
              ? "Revisa la propuesta y marca tu autorización para habilitar el render."
              : unknownCost && !byId("unknown-cost-consent").checked
                ? "El coste es desconocido; confírmalo expresamente para continuar."
                : "La propuesta está lista. El render solo empezará al pulsar el botón de aprobación.";
}

function renderStyle(style) {
  state.style = style;
  state.styleLoaded = true;
  const form = byId("style-form");
  for (const [key, value] of Object.entries(style || {})) {
    const field = form.elements.namedItem(key);
    if (!field) continue;
    if (key === "emoji_by_keyword") {
      field.value = value && typeof value === "object" ? JSON.stringify(value, null, 2) : "";
    } else if (field.type === "checkbox") {
      field.checked = value === true;
    } else if (field.type === "color") {
      field.value = /^#[0-9a-f]{6}$/i.test(value) ? value : "#000000";
    } else field.value = value == null ? "" : value;
  }
  byId("style-fields").disabled = false;
  byId("style-fields").hidden = false;
  byId("save-style-button").disabled = false;
  byId("style-message").textContent = "Estilo cargado. Puedes ajustar los valores y guardar los cambios.";
}

function collectStyle() {
  const data = {};
  const fields = byId("style-form").elements;
  for (const key of ["font_family", "font_size", "font_color", "highlight_color", "stroke_color", "stroke_width", "animation_style", "show_subtitles", "caption_position", "caption_offset_y", "emoji_by_keyword", "logo_path", "logo_position", "logo_scale", "logo_offset_x", "logo_offset_y", "output_fps", "max_video_duration_seconds", "max_transcription_chunks", "openai_price_per_audio_minute_usd", "shotstack_price_per_render_minute_usd", "max_estimated_cost_usd"]) {
    const field = fields.namedItem(key);
    if (!field) continue;
    if (key === "emoji_by_keyword") {
      const raw = field.value.trim();
      if (!raw) data[key] = null;
      else {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || Object.values(parsed).some((value) => typeof value !== "string")) {
          throw new Error("Las reglas de emoji deben ser un objeto JSON con valores de texto.");
        }
        data[key] = parsed;
      }
    } else if (field.type === "checkbox") data[key] = field.checked;
    else if (field.type === "number") data[key] = field.value === "" ? null : Number(field.value);
    else if (key === "logo_path") data[key] = field.value.trim() || null;
    else data[key] = field.value;
  }
  // Preserve backend style keys this UI does not edit, without inventing values.
  for (const [key, value] of Object.entries(state.style || {})) {
    if (!(key in data)) data[key] = value;
  }
  return data;
}

async function refreshWorkspace() {
  byId("refresh-button").disabled = true;
  byId("refresh-button").textContent = "Actualizando…";
  try {
    const results = await Promise.allSettled([loadHealth(), loadLibrary(), loadDriveStatus()]);
    const healthResult = results[0];
    const libraryResult = results[1];
    const driveResult = results[2];
    const errors = [];
    if (healthResult.status === "fulfilled") renderHealth(healthResult.value);
    else {
      byId("connection-label").textContent = "No se pudo comprobar";
      $(".local-indicator").classList.remove("connected");
      for (const id of ["health-openai", "health-modal", "health-environment", "health-drive"]) {
        const status = byId(id);
        status.textContent = "No disponible";
        status.className = "health-unknown";
      }
      errors.push(`No se pudo consultar el estado del servicio: ${healthResult.reason.message}`);
    }
    if (driveResult.status === "fulfilled") renderDriveStatus(driveResult.value);
    else {
      renderDriveStatus(null);
      errors.push(`No se pudo consultar el estado de Google Drive: ${driveResult.reason.message}`);
    }
    if (libraryResult.status === "fulfilled") {
      state.library = libraryResult.value;
      state.libraryLoaded = true;
      ensureVisibleSelection();
      renderLibrary();
      renderSelectedItem();
    } else {
      byId("library-count").textContent = "No se pudo consultar";
      byId("summary-pending-note").textContent = "No se pudo consultar";
      byId("summary-source").textContent = "No se pudo consultar la biblioteca";
      errors.push(`No se pudo consultar la biblioteca: ${libraryResult.reason.message}`);
    }
    if (errors.length) displayToast(errors.join(" · "), true);
    else displayToast(libraryRefreshMessage());
  } finally {
    byId("refresh-button").disabled = false;
    byId("refresh-button").innerHTML = 'Actualizar datos <span>↻</span>';
  }
}

async function pollJob(jobId, sourceName) {
  try {
    const job = await loadJob(jobId);
    renderJob(job, sourceName);
  } catch (error) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    byId("job-content").insertAdjacentHTML("beforeend", `<div class="job-error">No se pudo consultar el estado del procesamiento: ${escapeHtml(error.message)}. Usa «Actualizar datos» para volver a intentarlo.</div>`);
    displayToast(`No se pudo consultar el procesamiento: ${error.message}`, true);
  }
}

byId("today-label").textContent = new Intl.DateTimeFormat("es", { day: "2-digit", month: "short", year: "numeric" }).format(new Date()).toLocaleUpperCase("es");
byId("connect-button").addEventListener("click", refreshWorkspace);
byId("refresh-button").addEventListener("click", refreshWorkspace);
byId("drive-connect-button").addEventListener("click", async () => {
  const button = byId("drive-connect-button");
  const message = byId("drive-oauth-message");
  button.disabled = true;
  message.hidden = false;
  message.dataset.error = "false";
  message.textContent = "Preparando la autorización segura de Google…";
  try {
    const result = await startDriveOAuth();
    if (!result?.authorization_url) throw new Error("El servidor no devolvió la URL de autorización.");
    window.location.assign(result.authorization_url);
  } catch (error) {
    message.dataset.error = "true";
    message.textContent = `No se inició la conexión: ${error.message}`;
    button.disabled = state.driveStatus?.ready !== true;
  }
});
byId("drive-scan-button").addEventListener("click", async () => {
  const button = byId("drive-scan-button");
  const message = byId("drive-oauth-message");
  button.disabled = true;
  message.hidden = false;
  message.dataset.error = "false";
  message.textContent = "Consultando únicamente 01_ENTRADA y RECURSOS/LOGOS configuradas. No se iniciará ningún render.";
  try {
    const result = await scanDrive();
    state.library = Array.isArray(result?.items) ? result.items : await loadLibrary();
    state.libraryLoaded = true;
    ensureVisibleSelection();
    renderLibrary();
    renderSelectedItem();
    const failures = Array.isArray(result?.failed) ? result.failed.length : 0;
    message.textContent = `Escaneo completado: ${result?.imported?.length || 0} vídeo(s) nuevo(s), ${result?.skipped || 0} ya importado(s), ${failures} error(es). No se llamó a OpenAI ni se inició ningún render.`;
    if (failures) {
      message.dataset.error = "true";
      displayToast("El escaneo terminó con errores. Revisa el detalle del panel.", true);
    } else {
      displayToast("Escaneo de Drive completado. No se inició ningún render.");
    }
  } catch (error) {
    message.dataset.error = "true";
    message.textContent = `No se pudo escanear Drive: ${error.message}`;
    displayToast(`No se pudo escanear Drive: ${error.message}`, true);
  } finally {
    button.disabled = state.driveStatus?.authorized !== true || state.driveStatus?.ready !== true;
  }
});
byId("drive-write-test-button").addEventListener("click", async () => {
  const button = byId("drive-write-test-button");
  const message = byId("drive-oauth-message");
  const confirmed = window.confirm(
    "Se creará un MP4 temporal pequeño en 02_PROCESADOS y, si Drive confirma la creación, MATRIX MOTION borrará únicamente ese archivo. Si falla la limpieza, el panel mostrará su identificador para retirarlo manualmente. ¿Continuar?"
  );
  if (!confirmed) return;
  button.disabled = true;
  message.hidden = false;
  message.dataset.error = "false";
  message.textContent = "Preparando la prueba de escritura en 02_PROCESADOS…";
  try {
    const result = await testDriveWrite();
    if (result?.created_and_deleted !== true) throw new Error("Drive no confirmó la creación y limpieza del MP4 temporal.");
    message.textContent = "Prueba de escritura superada: Drive creó y MATRIX MOTION borró el MP4 temporal en 02_PROCESADOS.";
    displayToast("Prueba de escritura completada. No se inició ningún render.");
  } catch (error) {
    message.dataset.error = "true";
    message.textContent = `No se pudo completar la prueba de escritura: ${error.message}`;
    displayToast(`Prueba de escritura fallida: ${error.message}`, true);
  } finally {
    button.disabled = !canWriteDriveProbe();
  }
});
byId("retry-drive-upload-button").addEventListener("click", async () => {
  const item = getSelectedItem();
  if (!item) return;
  const button = byId("retry-drive-upload-button");
  button.disabled = true;
  try {
    const result = await retryDriveUpload(item.id);
    await refreshWorkspace();
    displayToast(result?.render_reused ? "Resultado subido a Drive reutilizando el render existente." : "Subida completada.");
  } catch (error) {
    displayToast(`No se pudo reintentar solo la subida a Drive: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});
document.querySelectorAll("[data-page]").forEach((button) => {
  button.addEventListener("click", () => setActivePage(button.dataset.page));
});

byId("library-list").addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-action]");
  if (actionButton?.dataset.action === "upload") {
    setActivePage("create");
    byId("video-input").click();
    return;
  }
  const recentButton = event.target.closest("[data-recent-id]");
  if (recentButton) {
    state.selectedId = recentButton.dataset.recentId;
    setActivePage("create");
    renderLibrary();
    renderSelectedItem();
    return;
  }
  const itemButton = event.target.closest(".library-item");
  if (!itemButton) return;
  state.selectedId = itemButton.dataset.id;
  byId("paid-consent").checked = false;
  byId("unknown-cost-consent").checked = false;
  renderLibrary();
  renderSelectedItem();
});
const uploadController = window.MatrixMotionUploadController.createUploadController({
  upload: uploadVideo,
  loadLibrary,
  getLibrary: () => state.library,
  setLibrary: (items) => {
    state.library = items;
    state.libraryLoaded = true;
  },
  setSelectedId: (id) => { state.selectedId = id; },
  renderLibrary,
  renderSelectedItem,
  getNiche: () => byId("upload-niche").value,
  getEditingStyle: () => byId("upload-editing-style").value,
  getStyleNotes: () => byId("upload-style-notes").value.trim(),
  clearStyleNotes: () => { byId("upload-style-notes").value = ""; },
  setBusy: (busy) => {
    state.uploadingVideo = busy;
    byId("video-input").disabled = busy;
    byId("upload-trigger").disabled = busy;
    document.querySelectorAll("[data-action='upload']").forEach((button) => { button.disabled = busy; });
    byId("upload-trigger").setAttribute("aria-busy", String(busy));
  },
  setMessage: (text, { error = false, busy = false } = {}) => {
    const message = byId("upload-message");
    message.hidden = false;
    message.classList.toggle("error", error);
    message.setAttribute("aria-busy", String(busy));
    message.textContent = text;
  },
  notify: displayToast,
});
window.MatrixMotionUploadController.bindFileInput(byId("video-input"), uploadController);
byId("upload-trigger").addEventListener("click", () => {
  if (!state.uploadingVideo) byId("video-input").click();
});
document.querySelectorAll("[data-niche]").forEach((button) => {
  button.addEventListener("click", () => {
    byId("upload-niche").value = button.dataset.niche;
    document.querySelectorAll("[data-niche]").forEach((candidate) => {
      const selected = candidate === button;
      candidate.classList.toggle("selected", selected);
      candidate.setAttribute("aria-pressed", String(selected));
    });
  });
});
document.querySelectorAll("[data-editing-style]").forEach((button) => {
  button.addEventListener("click", () => {
    const editingStyle = button.dataset.editingStyle;
    byId("upload-editing-style").value = editingStyle;
    document.querySelectorAll("[data-editing-style]").forEach((candidate) => {
      const selected = candidate === button;
      candidate.classList.toggle("selected", selected);
      candidate.setAttribute("aria-pressed", String(selected));
    });
    if (getSelectedItem()) {
      byId("edit-subtitle-mode").value = editingStyle === "natural_premium" ? "none" : "dynamic";
      invalidateEditOptions();
    }
  });
});
const videoDropzone = byId("video-dropzone");
videoDropzone.addEventListener("keydown", (event) => {
  if ((event.key === "Enter" || event.key === " ") && event.target === videoDropzone) {
    event.preventDefault();
    byId("video-input").click();
  }
});
videoDropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  videoDropzone.classList.add("drag-over");
});
videoDropzone.addEventListener("dragleave", () => videoDropzone.classList.remove("drag-over"));
videoDropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  videoDropzone.classList.remove("drag-over");
  const file = event.dataTransfer?.files?.[0];
  if (file && !state.uploadingVideo) uploadController.handleSelection(file);
});
videoDropzone.addEventListener("click", (event) => {
  if (event.target.closest("button, input") || state.uploadingVideo) return;
  byId("video-input").click();
});
byId("recent-video-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-recent-id]");
  if (!button) return;
  state.selectedId = button.dataset.recentId;
  byId("paid-consent").checked = false;
  byId("unknown-cost-consent").checked = false;
  setActivePage("create");
  renderLibrary();
  renderSelectedItem();
});

byId("paid-consent").addEventListener("change", updateProcessButton);
byId("unknown-cost-consent").addEventListener("change", updateProcessButton);
syncRenderBackendControls();
  for (const controlId of ["edit-render-backend", "edit-subtitle-mode", "edit-output-fps", "edit-output-profile", "edit-output-quality"]) {
  byId(controlId).addEventListener("change", () => {
      if (controlId === "edit-render-backend") syncRenderBackendControls();
      invalidateEditOptions();
      updateCreationSummary();
  });
}
for (const controlId of [
  "custom-captions-enabled", "custom-caption-color", "custom-caption-timing",
  "custom-caption-size", "custom-caption-position", "custom-autozoom",
  "custom-transitions", "custom-original-volume", "custom-noise-reduction",
  "custom-music-enabled", "custom-music-ducking", "custom-layer-titles",
  "custom-layer-accents", "custom-image-filter", "custom-free-instructions",
]) {
  const control = byId(controlId);
  const eventName = control.type === "range" || control.tagName === "TEXTAREA" ? "input" : "change";
  control.addEventListener(eventName, () => {
    invalidateEditOptions();
    updateCreationSummary();
  });
}
  byId("add-cut-button").addEventListener("click", () => addCreativePlanRow("cut"));
  byId("add-zoom-button").addEventListener("click", () => addCreativePlanRow("zoom"));
  byId("add-sticker-button").addEventListener("click", () => addCreativePlanRow("sticker"));
async function prepareProposalForMode(renderMode = "full") {
  const item = getSelectedItem();
  if (!item) {
    displayToast("Selecciona un vídeo antes de continuar.", true);
    return;
  }
  const button = byId("process-button");
  button.disabled = true;
  button.textContent = "Preparando propuesta…";
  const summary = byId("proposal-summary");
  summary.hidden = false;
  summary.textContent = "Validando el vídeo y calculando el plan; no se llamará a ningún proveedor.";
  try {
    const result = await prepareEditProposal(
      item.id,
      byId("custom-captions-enabled").checked ? "dynamic" : "none",
      byId("edit-output-fps").value,
      byId("edit-output-profile").value,
      byId("edit-output-quality").value,
      collectCreativePlan(),
      byId("edit-render-backend").value,
      renderMode,
      collectPanelCustomization(),
    );
    renderEditProposal(result);
    updateCreationSummary(item);
    displayToast(
      `${renderMode === "quick_demo" ? "Prueba de 5 segundos" : "Propuesta completa"} preparada sin llamadas externas. Revisa los permisos y el coste antes de iniciar servicios.`,
    );
  } catch (error) {
    summary.hidden = false;
    summary.textContent = `No se pudo preparar la propuesta: ${error.message}`;
    byId("render-approval").hidden = true;
    displayToast(`No se pudo preparar la propuesta: ${error.message}`, true);
  } finally {
    button.textContent = "Preparar propuesta local";
    updateProcessButton();
  }
}

byId("process-button").addEventListener("click", () => prepareProposalForMode("full"));

async function handleStep5Action(renderMode) {
  const item = getSelectedItem();
  if (!item) {
    displayToast("Selecciona un vídeo antes de continuar.", true);
    return;
  }
  const proposal = state.editProposal;
  const matches =
    proposal?.source_id === item.id &&
    proposal?.edit?.render_mode === renderMode;
  if (!matches) {
    await prepareProposalForMode(renderMode);
    return;
  }
  byId("approve-render-button").click();
}

byId("create-video-button").addEventListener("click", () => handleStep5Action("full"));
byId("quick-demo-button").addEventListener("click", () => handleStep5Action("quick_demo"));

byId("approve-render-button").addEventListener("click", async () => {
  const proposal = state.editProposal;
  if (!proposal) {
    displayToast("Prepara y revisa primero una propuesta.", true);
    return;
  }
  const unknown = proposal.costs?.total_estimate_usd === null
    || proposal.costs?.total_estimate_usd === undefined;
  if (!byId("paid-consent").checked || (unknown && !byId("unknown-cost-consent").checked)) {
    displayToast("Falta confirmar la autorización y el coste desconocido.", true);
    return;
  }
  const providerSummary =
    "Se procesará solo el audio necesario para la transcripción; el vídeo se analizará para sugerir momentos y estrategia de contenido; el render usará la fuente maestra y el MP4 se guardará en almacenamiento privado.";
  const costSummary = unknown
    ? "El coste total no está disponible y podría haber cargos."
    : `La estimación total es ${formatCost(proposal.costs.total_estimate_usd)}.`;
  if (!window.confirm(`¿Apruebas esta propuesta de ${proposal.source_name}? ${providerSummary} ${costSummary}`)) {
    return;
  }
  const item = getSelectedItem();
  const button = byId("approve-render-button");
  button.disabled = true;
  button.querySelector("span").textContent = "Iniciando render…";
  try {
    const result = await startProcessing(proposal, unknown);
    const job = result?.job || result;
    state.activeJob = job;
    renderJob(job, item.original_filename || item.filename);
    if (job.id && !["completed", "complete", "failed", "error", "rejected"].includes(String(job.status || "").toLowerCase())) {
      clearInterval(state.pollTimer);
      state.pollTimer = setInterval(() => pollJob(job.id, item.original_filename || item.filename), 2500);
      await pollJob(job.id, item.original_filename || item.filename);
    }
    displayToast("Render solicitado con tu aprobación expresa.");
  } catch (error) {
    displayToast(`No se pudo iniciar el render: ${error.message}`, true);
    byId("job-status-tag").textContent = "ERROR AL INICIAR";
    byId("job-content").innerHTML = `<div class="job-error">${escapeHtml(error.message)}</div>`;
  } finally {
    button.querySelector("span").textContent = "Autorizar y crear";
    updateProcessButton();
  }
});

byId("load-style-button").addEventListener("click", async () => {
  const button = byId("load-style-button");
  button.disabled = true;
    byId("style-message").textContent = "Consultando el estilo…";
  try {
    renderStyle(await loadStyle());
  } catch (error) {
    byId("style-message").textContent = `No se pudo consultar el estilo: ${error.message}`;
    displayToast(`No se pudo consultar el estilo: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});
byId("style-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.styleLoaded) {
    displayToast("Consulta primero el estilo antes de guardar los cambios.", true);
    return;
  }
  const button = byId("save-style-button");
  button.disabled = true;
  byId("style-message").textContent = "Guardando cambios…";
  try {
    const saved = await saveStyle(collectStyle());
    renderStyle(saved || collectStyle());
    state.library = await loadLibrary();
    state.libraryLoaded = true;
    renderLibrary();
    renderSelectedItem();
    byId("style-message").textContent = "Cambios guardados.";
    displayToast("Estilo visual guardado.");
  } catch (error) {
    byId("style-message").textContent = `No se pudieron guardar los cambios: ${error.message}`;
    displayToast(`Error al guardar el estilo: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});

byId("logout-button")?.addEventListener("click", async () => {
  const button = byId("logout-button");
  button.disabled = true;
  try {
    await requestJson("/logout", { method: "POST" });
    window.location.replace("/login");
  } catch (error) {
    displayToast(`No se pudo cerrar la sesión: ${error.message}`, true);
    button.disabled = false;
  }
});

async function initializePanel() {
  try {
    const session = await requestJson("/api/session");
    csrfToken = String(session?.csrf_token || "");
    if (!csrfToken) throw new Error("No se pudo validar la sesión del panel.");
    await refreshWorkspace();
    await loadDriveConfig();
  } catch (error) {
    if (!window.location.pathname.startsWith("/login")) {
      displayToast(error.message, true);
    }
  }
}

async function listDriveFiles(role, token = null) {
  const result = await requestJson(API.driveList, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, access_token: token || await getDriveAccessToken() }),
  });
  renderDriveFiles(role, result.files || []);
  return result.files || [];
}

async function getDriveAccessToken() {
  if (state.driveToken && Date.now() < state.driveTokenExpiresAt - 60_000) return state.driveToken;
    const clientId = state.drive?.picker?.client_id;
  if (!clientId) throw new Error("Falta configurar GOOGLE_DRIVE_CLIENT_ID en las variables protegidas.");
  await loadScript("https://accounts.google.com/gsi/client");
  return new Promise((resolve, reject) => {
    state.googleTokenClient = google.accounts.oauth2.initTokenClient({
      client_id: clientId,
      scope: "https://www.googleapis.com/auth/drive.file openid email",
      callback: (response) => {
        if (response.error || !response.access_token) {
          state.driveToken = null;
          reject(new Error(response.error_description || "Google no concedió el acceso solicitado."));
          return;
        }
        state.driveToken = response.access_token;
        state.driveTokenExpiresAt = Date.now() + Number(response.expires_in || 3600) * 1000;
        resolve(state.driveToken);
      },
    });
    state.googleTokenClient.requestAccessToken({ prompt: "select_account" });
  });
}

function renderDriveFiles(role, files) {
  const container = byId(`drive-files-${role}`);
  container.innerHTML = files.length
    ? `<ul>${files.map((file) => `<li><span>${escapeHtml(file.name)}</span><small>${escapeHtml(file.size ? formatBytes(file.size) : file.mimeType || "")}</small></li>`).join("")}</ul>`
    : "<small>Sin archivos autorizados. Usa «Autorizar» para elegirlos en Picker.</small>";
  byId(`drive-count-${role}`).textContent = `${files.length} archivo(s) autorizado(s)`;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = [...document.scripts].find((script) => script.src === src);
    if (existing?.dataset.loaded === "true") return resolve();
    if (existing) {
      existing.addEventListener("load", resolve, { once: true });
      existing.addEventListener("error", reject, { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => { script.dataset.loaded = "true"; resolve(); };
    script.onerror = () => reject(new Error("No se pudieron cargar las bibliotecas de Google Picker."));
    document.head.append(script);
  });
}

async function openDrivePicker(role, selectFiles = false) {
  const pickerSettings = state.drive?.picker;
  if (!pickerSettings?.client_id || !pickerSettings?.api_key || !pickerSettings?.app_id) {
    throw new Error("Configura primero las credenciales de Google Picker en las variables protegidas.");
  }
  const accessToken = await getDriveAccessToken();
  await loadScript("https://apis.google.com/js/api.js");
  await new Promise((resolve, reject) => {
    gapi.load("picker", { callback: resolve, onerror: () => reject(new Error("No se pudo iniciar Google Picker.")) });
  });
  let view;
  if (selectFiles) {
    const folderRole = role === "instrucciones" ? "entrada" : role;
    const folder = state.drive?.folders?.[folderRole];
    if (!folder) throw new Error("Selecciona primero la carpeta correspondiente.");
    view = new google.picker.DocsView(google.picker.ViewId.DOCS)
      .setParent(folder.id)
      .setIncludeFolders(false)
      .setMimeTypes(role === "entrada"
        ? "video/mp4"
        : role === "instrucciones"
          ? "application/vnd.google-apps.document"
          : "image/png,image/jpeg,image/webp");
  } else {
    view = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
      .setIncludeFolders(true)
      .setSelectFolderEnabled(true);
  }
  let builder = new google.picker.PickerBuilder()
    .setAppId(pickerSettings.app_id)
    .setDeveloperKey(pickerSettings.api_key)
    .setOAuthToken(accessToken)
    .setTitle(selectFiles
      ? `Autoriza ${role === "entrada" ? "MP4" : role === "instrucciones" ? "instrucciones" : "logos"} de ${state.drive.folders[role === "instrucciones" ? "entrada" : role].name}`
      : `Selecciona la carpeta ${role.toUpperCase()}`)
    .addView(view)
    .setCallback(async (data) => {
      if (data.action !== google.picker.Action.PICKED) return;
      const selectedDocs = data.docs || [];
      const selected = selectedDocs[0];
      if (!selected?.id) {
        byId("drive-message").textContent = "Picker no devolvió una selección válida.";
        return;
      }
      if (selectFiles) {
        byId("drive-message").textContent = `Validando ${selectedDocs.length} archivo(s) seleccionados…`;
        try {
          const result = await requestJson("/api/drive/files/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              role,
              files: selectedDocs.map((file) => ({ id: file.id })),
              access_token: accessToken,
            }),
          });
          state.drive.files[role] = result.files || [];
          renderDriveFiles(role, state.drive.files[role]);
          await loadDriveConfig();
          byId("drive-message").textContent = `${result.files.length} archivo(s) autorizados explícitamente. Los archivos nuevos deben seleccionarse después.`;
        } catch (error) {
          byId("drive-message").textContent = `No se pudo autorizar la selección: ${error.message}`;
          byId("drive-message").classList.add("error");
        }
        return;
      }
      byId("drive-message").textContent = `Comprobando acceso a ${selected.name}…`;
      try {
        const result = await requestJson(API.driveFolders, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role, folder_id: selected.id, access_token: accessToken }),
        });
        state.drive.folders = result.folders;
        await loadDriveConfig();
        byId(`drive-count-${role}`).textContent = role === "procesados"
          ? "Carpeta accesible. La escritura se prueba por separado."
          : "Carpeta accesible. Elige los archivos que autorizarás.";
        byId("drive-message").textContent = `Carpeta ${selected.name} seleccionada y acceso verificado.`;
      } catch (error) {
        byId("drive-message").textContent = `No se pudo verificar la carpeta: ${error.message}`;
        byId("drive-message").classList.add("error");
      }
    })
  builder.enableFeature(google.picker.Feature.SUPPORT_DRIVES);
  if (selectFiles) builder.enableFeature(google.picker.Feature.MULTISELECT_ENABLED);
  const picker = builder.build();
  picker.setVisible(true);
}

async function loadDriveConfig() {
  try {
    state.drive = await requestJson(API.driveConfig);
    const picker = state.drive?.picker || {};
    const configured = Boolean(picker.client_id && picker.api_key && picker.app_id);
    for (const role of ["entrada", "logos", "procesados"]) {
      const folder = state.drive.folders?.[role];
      byId(`drive-folder-${role}`).textContent = folder?.name || "Sin seleccionar";
      byId(`drive-count-${role}`).textContent = folder
        ? `Carpeta seleccionada · ID ${folder.id}`
        : role === "procesados" ? "Solo se crean archivos nuevos aquí." : "Elige una carpeta para consultar archivos.";
    }
    for (const role of ["entrada", "logos", "instrucciones"]) {
      const folderRole = role === "instrucciones" ? "entrada" : role;
      if (state.drive.folders?.[folderRole]) {
        renderDriveFiles(role, state.drive.files?.[role] || []);
      } else {
        byId(`drive-files-${role}`).innerHTML = "";
      }
    }
    document.querySelectorAll(".drive-pick-button").forEach((button) => {
      button.disabled = !configured;
    });
    document.querySelectorAll(".drive-authorize-button").forEach((button) => {
      const role = button.dataset.driveAuthorize;
      const folderRole = role === "instrucciones" ? "entrada" : role;
      button.disabled = !configured || !state.drive.folders?.[folderRole];
    });
    document.querySelectorAll(".drive-list-button").forEach((button) => {
      const role = button.dataset.driveList;
      const folderRole = role === "instrucciones" ? "entrada" : role;
      button.disabled = !configured || !state.drive.folders?.[folderRole] || !(state.drive.files?.[role]?.length);
    });
    byId("drive-test-file-button").disabled = !configured || !state.drive.folders?.procesados;
    byId("drive-write-test-button").disabled = !canWriteDriveProbe();
    byId("drive-sync-button").disabled = !configured || !state.drive.folders?.entrada || !state.drive.folders?.logos;
    byId("drive-message").classList.remove("error");
    byId("drive-message").textContent = configured
      ? "Picker opcional para seleccionar archivos. El escaneo automático usa OAuth de servidor y los IDs fijos de las carpetas."
      : "Picker opcional no configurado; esto no afecta a la conexión OAuth ni al escaneo automático por IDs fijos.";
  } catch (error) {
    byId("drive-message").textContent = `No se pudo consultar la configuración de Drive: ${error.message}`;
    byId("drive-message").classList.add("error");
  }
}

document.querySelectorAll(".drive-pick-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const role = button.dataset.drivePick;
    button.disabled = true;
    try {
      await openDrivePicker(role);
    } catch (error) {
      byId("drive-message").textContent = `No se pudo abrir Google Picker: ${error.message}`;
      byId("drive-message").classList.add("error");
    } finally {
      button.disabled = !state.drive?.picker?.client_id
        || !state.drive?.picker?.api_key
        || !state.drive?.picker?.app_id;
    }
  });
});

document.querySelectorAll(".drive-authorize-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const role = button.dataset.driveAuthorize;
    button.disabled = true;
    try {
      await openDrivePicker(role, true);
    } catch (error) {
      byId("drive-message").textContent = `No se pudo seleccionar archivos: ${error.message}`;
      byId("drive-message").classList.add("error");
    } finally {
      button.disabled = !state.drive?.picker?.client_id
        || !state.drive?.picker?.api_key
        || !state.drive?.picker?.app_id
        || !state.drive?.folders?.[role === "instrucciones" ? "entrada" : role];
    }
  });
});

document.querySelectorAll(".drive-list-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const role = button.dataset.driveList;
    button.disabled = true;
    try {
      const files = await listDriveFiles(role);
      byId("drive-message").textContent = `${files.length} archivo(s) seleccionados y accesibles en ${role}.`;
      byId("drive-message").classList.remove("error");
    } catch (error) {
      byId("drive-message").textContent = `No se pudo consultar la selección: ${error.message}`;
      byId("drive-message").classList.add("error");
    } finally {
      button.disabled = !state.drive?.picker?.client_id
        || !state.drive?.picker?.api_key
        || !state.drive?.picker?.app_id
        || !state.drive?.folders?.[role === "instrucciones" ? "entrada" : role]
        || !(state.drive?.files?.[role]?.length);
    }
  });
});

byId("copy-seo-button")?.addEventListener("click", (event) => {
  void copySeoStrategy(event.currentTarget);
});

byId("drive-sync-button").addEventListener("click", async () => {
  const button = byId("drive-sync-button");
  button.disabled = true;
  byId("drive-message").classList.remove("error");
  byId("drive-message").textContent = "Descargando solo los archivos elegidos en Picker…";
  try {
    const result = await postDrive(API.driveSync);
    state.library = await loadLibrary();
    state.libraryLoaded = true;
    ensureVisibleSelection();
    renderLibrary();
    renderSelectedItem();
    const items = [...(result.videos || []), ...(result.logos || [])];
    const downloaded = items.filter((item) => item.status === "downloaded").length;
    const unchanged = items.filter((item) => item.status === "already_downloaded").length;
    const invalid = (result.videos || []).filter((item) => item.library_error).length;
    byId("drive-message").textContent = `Sincronización completada: ${downloaded} descargado(s), ${unchanged} ya sincronizado(s), ${invalid} error(es) de biblioteca.`;
  } catch (error) {
    byId("drive-message").textContent = `No se pudieron descargar los archivos elegidos: ${error.message}`;
    byId("drive-message").classList.add("error");
  } finally {
    button.disabled = !state.drive?.picker?.client_id
      || !state.drive?.picker?.api_key
      || !state.drive?.picker?.app_id
      || !state.drive?.folders?.entrada
      || !state.drive?.folders?.logos;
  }
});

byId("drive-test-file-button").addEventListener("click", async () => {
  const button = byId("drive-test-file-button");
  button.disabled = true;
  byId("drive-message").classList.remove("error");
  byId("drive-message").textContent = "Creando un archivo nuevo de prueba en Procesados…";
  try {
    const result = await postDrive(API.driveTestFile);
    byId("drive-message").textContent = `Archivo nuevo creado: ${result.name || result.id}. No se modificó ningún original.`;
  } catch (error) {
    byId("drive-message").textContent = `No se pudo crear el archivo de prueba: ${error.message}`;
    byId("drive-message").classList.add("error");
  } finally {
    button.disabled = !state.drive?.picker?.client_id
      || !state.drive?.picker?.api_key
      || !state.drive?.picker?.app_id
      || !state.drive?.folders?.procesados;
  }
});

setActivePage("home");
initializePanel();
void initializeMasterPreview();
