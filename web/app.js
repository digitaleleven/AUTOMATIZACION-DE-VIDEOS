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
};

let csrfToken = "";

const $ = (selector) => document.querySelector(selector);
const byId = (id) => document.getElementById(id);

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

async function uploadVideo(file) {
  return requestJson(API.upload, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Upload-Filename": encodeURIComponent(file.name),
    },
    body: file,
  });
}

async function prepareEditProposal(sourceId, subtitleMode, outputFps, outputProfile, outputQuality, creativePlan) {
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
    }),
  });
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

async function startProcessing(proposalId, authorizePaidApis, unknownCostAcknowledged) {
  return requestJson(API.jobs, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      proposal_id: proposalId,
      authorize_paid_apis: authorizePaidApis,
      unknown_cost_acknowledged: unknownCostAcknowledged,
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
  return state.library.find((item) => String(item.id) === String(state.selectedId)) || null;
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

function displayToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
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
    normalizing: "Preparando el formato del vídeo", preparing_captions: "Preparando subtítulos",
    uploading: "Enviando el vídeo a Shotstack Stage",
    uploading_captions: "Enviando los subtítulos a Shotstack Stage",
    validating_output: "Comprobando el resultado", interrupted: "Interrumpido",
  };
  return labels[String(status || "").toLowerCase()] || (status ? String(status) : "No disponible");
}

function renderLibrary() {
  const list = byId("library-list");
  byId("library-count").textContent = state.library.length ? `${state.library.length} ARCHIVO${state.library.length === 1 ? "" : "S"}` : "0 ARCHIVOS";
  renderSummary();
  if (!state.library.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-glyph">▧</div><h3>Tu biblioteca está vacía</h3><p>${state.libraryLoaded ? "Todavía no hay vídeos. Añade un archivo MP4 o MOV para empezar." : "Añade tu primer vídeo para empezar a crear contenido."}</p><button class="outline-button" type="button" data-action="upload">Añadir un vídeo</button></div>`;
    return;
  }
  list.innerHTML = state.library.map((item) => {
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

function renderSummary() {
  if (!state.libraryLoaded) return;
  const pendingStatuses = new Set(["ready", "pending", "queued", "processing", "transcribing", "rendering", "downloading", "normalizing", "preparing_captions", "uploading", "uploading_captions", "validating_output"]);
  const processedStatuses = new Set(["completed", "complete"]);
  const errorStatuses = new Set(["failed", "error", "rejected", "interrupted"]);
  const count = (statuses) => state.library.filter((item) => statuses.has(String(item.status || "").toLowerCase())).length;
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
  byId("edit-subtitle-mode").value = "none";
  byId("edit-output-profile").value = "1080p";
  byId("edit-output-quality").value = "high";
  const supportedFps = [25, 30, 50, 60];
  const sourceFps = Math.round(Number(item.fps) || 30);
  byId("edit-output-fps").value = String(supportedFps.includes(sourceFps) ? sourceFps : 30);
  resetCreativePlanControls();
  clearEditProposal();
}

function renderEditProposal(proposal) {
  state.editProposal = proposal;
  const modeLabels = {
    none: "Sin subtítulos",
    simple: "Subtítulos sencillos, sin animación",
    dynamic: "Subtítulos dinámicos con palabras destacadas",
  };
  const source = proposal.source || {};
  const edit = proposal.edit || {};
  const costs = proposal.costs || {};
  const apiPlan = proposal.api_plan || {};
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
    `Origen: ${source.origin || "No identificado"}`,
    `Entrada: ${source.width || "?"} × ${source.height || "?"} · ${source.fps || "?"} FPS · ${source.video_codec || "códec desconocido"}`,
    `Bitrate de entrada medido por FFprobe: ${sourceBitrate > 0 ? `${(sourceBitrate / 1000000).toFixed(2)} Mbps` : "no informado"}`,
    `Salida: MP4 ${edit.output_width || 1080} × ${edit.output_height || 1920} · ${edit.output_fps || "?"} FPS · calidad ${edit.output_quality || "?"}`,
    `Duración final prevista: ${Number.isFinite(outputDuration) ? `${outputDuration.toFixed(2)} s` : "no disponible"}`,
    `Subtítulos: ${modeLabels[edit.subtitle_mode] || "No disponible"}`,
    `OpenAI: ${apiPlan.openai_transcription ? `transcripción prevista (${apiPlan.openai_transcription_chunks} fragmento/s)` : "no se llamará; coste de transcripción 0"}`,
    `Audio: ${edit.keep_original_audio ? "se conserva el audio original (y se recorta junto al vídeo)" : "la entrada no tiene audio"}; no se añade música ni normalización`,
    `Logo: ${edit.logo_enabled ? "se usará el logo ya configurado" : "ninguno configurado"}`,
    `Encuadre: ${edit.framing || "no disponible"}`,
    `Cortes que se quitarán: ${cutsLabel}`,
    `Zooms: ${zoomsLabel}`,
    `Stickers con animación de zoom: ${stickersLabel}`,
    "El plan creativo de esta propuesta es manual; no se ha pedido a una IA que analice el contenido.",
    `Destino: ${proposal.destination || "no disponible"}`,
    `OpenAI estimado: ${formatCost(costs.openai_usd)}`,
    `Shotstack Stage estimado: ${formatCost(costs.shotstack_usd)}`,
    `Total estimado: ${formatCost(costs.total_estimate_usd)}`,
  ];
  const warnings = (proposal.warnings || []).filter(Boolean);
  const summary = byId("proposal-summary");
  summary.innerHTML = `<h3>Propuesta de edición</h3><ul>${lines.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>${warnings.length ? `<p>${warnings.map(escapeHtml).join(" ")}</p>` : ""}<small>La propuesta se calculó localmente. No se han llamado servicios externos. Caduca en 15 minutos.</small>`;
  summary.hidden = false;
  byId("cost-value").textContent = formatCost(costs.total_estimate_usd);
  byId("cost-note").textContent = costs.estimate_note || "No hay tarifas verificadas para calcular el coste.";
  byId("unknown-cost-wrap").hidden = !unknownCost;
  byId("unknown-cost-consent").checked = false;
  byId("paid-consent").checked = false;
  byId("paid-consent-copy").textContent = apiPlan.openai_transcription
    ? "Autorizo enviar este vídeo a OpenAI para transcribirlo y a Shotstack Stage para un único render."
    : "Autorizo enviar este vídeo a Shotstack Stage para un único render. No se usará OpenAI para transcribirlo.";
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
    byId("retry-render-button").hidden = true;
    byId("retry-render-hint").hidden = true;
    byId("retry-final-render-button").hidden = true;
    byId("retry-final-render-hint").hidden = true;
    updateProcessButton();
    return;
  }
  syncEditOptionsForSelectedItem(item);
  video.src = sourceMediaUrl(item.id);
  video.load();
  video.hidden = false;
  placeholder.hidden = true;
  byId("selected-status").textContent = statusLabel(item.status).toLocaleUpperCase("es");
  const fields = [
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
      ? "Estimación local para una edición sin subtítulos; revisa la propuesta exacta antes de autorizar."
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
  const retryButton = byId("retry-render-button");
  const retryHint = byId("retry-render-hint");
  retryButton.hidden = item.can_retry_render !== true;
  retryButton.disabled = false;
  retryHint.hidden = item.can_retry_render !== true;
  retryHint.textContent = item.retry_remote_status_check_available
    ? "Al autorizar, el servidor comprobará primero el estado del render registrado."
    : "No hay ID remoto: el estado en Shotstack no se puede consultar. Se solicitará confirmación antes de permitir el único reenvío.";
  const finalRetryButton = byId("retry-final-render-button");
  const finalRetryHint = byId("retry-final-render-hint");
  finalRetryButton.hidden = item.can_run_controlled_third_render !== true;
  finalRetryButton.disabled = false;
  finalRetryHint.hidden = item.can_run_controlled_third_render !== true;
  finalRetryHint.textContent = "Acción de una sola vez reservada para 0415.mov. Los dos intentos anteriores se conservarán.";
  updateProcessButton();
}

function renderHealth(health) {
  state.health = health;
  const set = (id, configured) => {
    const el = byId(id);
    el.textContent = configured === true ? "Configurada · sin verificar" : configured === false ? "No configurada" : "No disponible";
    el.className = configured === true ? "health-ok" : configured === false ? "health-no" : "health-unknown";
  };
  set("health-openai", health?.openai_key_configured);
  set("health-shotstack", health?.shotstack_key_configured);
  const env = health?.shotstack_environment;
  byId("health-environment").textContent = env == null || env === "" ? "No disponible" : String(env);
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
  state.activeJob = job;
  const source = state.library.find((item) => String(item.id) === String(job.source_id));
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
  byId("job-status-tag").textContent = statusLabel(job.status).toLocaleUpperCase("es");
  byId("job-content").innerHTML = `<div class="job-live">
    <div><strong>${escapeHtml(sourceName || "Procesamiento")}</strong><p>${escapeHtml(phase)} · Identificador ${escapeHtml(job.id ?? "No disponible")}</p>${progressMarkup}</div>
    <span class="job-progress-label">${escapeHtml(progressText)}</span>
    ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
    ${outputMetadataMarkup}
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
  const unavailable = ["completed", "processing", "queued", "failed"].includes(itemStatus);
  const proposal = state.editProposal;
  const unknownCost = !proposal
    || proposal.costs?.total_estimate_usd === null
    || proposal.costs?.total_estimate_usd === undefined;
  const consentOk = byId("paid-consent").checked
    && (!unknownCost || byId("unknown-cost-consent").checked);
  const needsOpenAI = proposal?.api_plan?.openai_transcription === true;
  const missingApi = proposal
    && (!proposal.api_plan?.shotstack_key_configured
      || (needsOpenAI && !proposal.api_plan?.openai_key_configured));
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
      for (const id of ["health-openai", "health-shotstack", "health-environment", "health-drive"]) {
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
      if (!state.library.some((item) => String(item.id) === String(state.selectedId))) {
        state.selectedId = state.library[0]?.id ?? null;
      }
      renderLibrary();
      renderSelectedItem();
    } else {
      byId("library-count").textContent = "No se pudo consultar";
      byId("summary-pending-note").textContent = "No se pudo consultar";
      byId("summary-source").textContent = "No se pudo consultar la biblioteca";
      errors.push(`No se pudo consultar la biblioteca: ${libraryResult.reason.message}`);
    }
    if (errors.length) displayToast(errors.join(" · "), true);
    else displayToast(state.library.length ? "Estado y biblioteca actualizados." : "Tu biblioteca está vacía. Añade un vídeo para empezar.");
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
    if (!state.library.some((item) => String(item.id) === String(state.selectedId))) {
      state.selectedId = state.library[0]?.id ?? null;
    }
    renderLibrary();
    renderSelectedItem();
    const failures = Array.isArray(result?.failed) ? result.failed.length : 0;
    message.textContent = `Escaneo completado: ${result?.imported?.length || 0} vídeo(s) nuevo(s), ${result?.skipped || 0} ya importado(s), ${failures} error(es). No se llamó a OpenAI ni a Shotstack.`;
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
async function authorizeRenderRetry(finalAttempt = false) {
  const item = getSelectedItem();
  const canAuthorize = finalAttempt
    ? item?.can_run_controlled_third_render
    : item?.can_retry_render;
  if (!canAuthorize || !item.latest_job_id) return;
  const settings = item.retry_settings || {};
  const plan = settings.creative_plan || {};
  const zooms = Array.isArray(plan.zooms) ? plan.zooms : [];
  const stickers = Array.isArray(plan.stickers) ? plan.stickers : [];
  const zoomSummary = zooms.length
    ? zooms.map((zoom) => `${Number(zoom.start_seconds).toFixed(0)}–${Number(zoom.end_seconds).toFixed(0)} s`).join(", ")
    : "ninguno";
  const stickerSummary = stickers.length
    ? stickers.map((sticker) => `${sticker.sticker === "sparkle" ? "Destello" : sticker.sticker}, ${Number(sticker.start_seconds).toFixed(1)} s durante ${Number(sticker.duration_seconds).toFixed(1)} s`).join("; ")
    : "ninguno";
  const outputLabel = settings.output_profile === "1080p" ? "1080 × 1920" : String(settings.output_profile || "configuración guardada");
  const estimatedCost = item.retry_estimated_cost_usd;
  const costLine = estimatedCost === null || estimatedCost === undefined
    ? "Coste estimado desconocido; el envío puede consumir créditos."
    : `Coste estimado guardado: ${formatCost(estimatedCost)}; el importe real puede variar.`;
  const driveWriteLine = item.retry_will_upload_to_drive
    ? "Al terminar, se creará un MP4 nuevo en 02_PROCESADOS. El original de Drive no se modificará."
    : "";
  const message = [
    finalAttempt
      ? `Se autorizará la única tercera y última prueba de RENDER-001 para ${item.original_filename || item.filename}.`
      : `Se autorizará un único reintento para ${item.original_filename || item.filename}.`,
    `Configuración guardada: ${settings.subtitle_mode === "none" ? "sin subtítulos" : "subtítulos " + settings.subtitle_mode}; ${outputLabel}, ${settings.output_fps} FPS, calidad ${settings.output_quality}; zoom ${zoomSummary}; sticker ${stickerSummary}.`,
    costLine,
    driveWriteLine,
    finalAttempt
      ? "Los dos intentos anteriores permanecerán intactos. Se enviará exactamente una vez a Shotstack Stage. ¿Autorizas esta prueba final?"
      : "Se enviará a Shotstack Stage. ¿Autorizas este único intento?",
  ].filter(Boolean).join("\n");
  if (!window.confirm(message)) return;
  const remoteStatusAvailable = item.retry_remote_status_check_available === true;
  if (!remoteStatusAvailable && !window.confirm(
    finalAttempt
      ? "Los dos intentos anteriores terminaron con HTTP 400 y no devolvieron un ID de render. No es posible consultar el estado remoto. MATRIX MOTION bloqueará cualquier cuarto intento. ¿Confirmas que autorizas exactamente esta tercera y última prueba?"
      : "El primer intento terminó con HTTP 400 y no devolvió un ID de render. No se puede consultar el estado remoto. MATRIX MOTION impedirá un segundo reintento local. ¿Confirmas que autorizas reenviar exactamente la edición guardada una vez?",
  )) return;
  const unknownCost = estimatedCost === null || estimatedCost === undefined;
  if (unknownCost && !window.confirm("El coste no puede estimarse con los datos guardados. ¿Aceptas continuar con coste desconocido?")) return;

  const button = byId(finalAttempt ? "retry-final-render-button" : "retry-render-button");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = finalAttempt ? "Preparando tercera prueba…" : "Solicitando reintento…";
  try {
    const retryPath = finalAttempt ? "retry-third" : "retry";
    const response = await requestJson(`/api/jobs/${encodeURIComponent(item.latest_job_id)}/${retryPath}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        authorize_paid_apis: true,
        confirm_single_retry: true,
        confirm_final_third_attempt: finalAttempt,
        confirm_no_render_id: !remoteStatusAvailable,
        confirm_drive_output_write: item.retry_will_upload_to_drive === true,
        unknown_cost_acknowledged: unknownCost,
      }),
    });
    const job = response?.job || response;
    state.activeJob = job;
    state.library = await loadLibrary();
    state.libraryLoaded = true;
    renderLibrary();
    renderSelectedItem();
    renderJob(job, item.original_filename || item.filename);
    if (job.id && !["completed", "complete", "failed", "error", "rejected"].includes(String(job.status || "").toLowerCase())) {
      clearInterval(state.pollTimer);
      state.pollTimer = setInterval(() => pollJob(job.id, item.original_filename || item.filename), 2500);
      await pollJob(job.id, item.original_filename || item.filename);
    }
    displayToast(response?.resumes_existing_render
      ? "Reanudada la descarga del render existente; no se creó otro render."
      : finalAttempt
        ? "Tercera y última prueba solicitada en Shotstack Stage."
        : "Reintento único solicitado en Shotstack Stage.");
  } catch (error) {
    displayToast(`No se pudo autorizar el reintento: ${error.message}`, true);
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}
byId("retry-render-button").addEventListener("click", () => authorizeRenderRetry(false));
byId("retry-final-render-button").addEventListener("click", () => authorizeRenderRetry(true));
document.querySelector(".list-toolbar [data-action='upload']")?.addEventListener("click", () => byId("video-input").click());
document.querySelectorAll("[data-scroll]").forEach((button) => button.addEventListener("click", () => {
  document.getElementById(button.dataset.scroll)?.scrollIntoView({ behavior: "smooth" });
  document.querySelectorAll("[data-scroll]").forEach((link) => link.classList.toggle("active", link.dataset.scroll === button.dataset.scroll));
}));

byId("library-list").addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-action]");
  if (actionButton?.dataset.action === "upload") {
    byId("video-input").click();
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
byId("upload-trigger").addEventListener("click", () => byId("video-input").click());
byId("video-input").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  const lowerName = file.name.toLowerCase();
  if (!lowerName.endsWith(".mp4") && !lowerName.endsWith(".mov")) {
    displayToast("Solo se admiten archivos .MP4 y .MOV.", true);
    event.target.value = "";
    return;
  }
  const message = byId("upload-message");
  message.hidden = false;
  message.textContent = `Guardando ${file.name} en la aplicación…`;
  try {
    const uploaded = await uploadVideo(file);
    const returnedId = uploaded?.id ?? uploaded?.item?.id ?? uploaded?.source_id;
    const freshLibrary = await loadLibrary();
    state.library = freshLibrary;
    state.libraryLoaded = true;
    state.selectedId = returnedId ?? freshLibrary.find((item) => item.filename === file.name || item.original_filename === file.name)?.id ?? freshLibrary[0]?.id ?? null;
    renderLibrary();
    renderSelectedItem();
    message.textContent = "Vídeo guardado. Revisa sus datos y el coste estimado antes de iniciar el procesamiento.";
    displayToast("Vídeo añadido a la biblioteca.");
  } catch (error) {
    message.textContent = `No se pudo cargar el vídeo: ${error.message}`;
    displayToast(`Error de carga: ${error.message}`, true);
  } finally {
    event.target.value = "";
  }
});

byId("paid-consent").addEventListener("change", updateProcessButton);
byId("unknown-cost-consent").addEventListener("change", updateProcessButton);
  for (const controlId of ["edit-subtitle-mode", "edit-output-fps", "edit-output-profile", "edit-output-quality"]) {
  byId(controlId).addEventListener("change", () => {
      invalidateEditOptions();
  });
}
  byId("add-cut-button").addEventListener("click", () => addCreativePlanRow("cut"));
  byId("add-zoom-button").addEventListener("click", () => addCreativePlanRow("zoom"));
  byId("add-sticker-button").addEventListener("click", () => addCreativePlanRow("sticker"));
byId("process-button").addEventListener("click", async () => {
  const item = getSelectedItem();
  if (!item) return;
  const button = byId("process-button");
  button.disabled = true;
  button.textContent = "Preparando propuesta…";
  const summary = byId("proposal-summary");
  summary.hidden = false;
  summary.textContent = "Validando el vídeo localmente y calculando el plan; no se llamará a ningún proveedor.";
  try {
    const result = await prepareEditProposal(
      item.id,
      byId("edit-subtitle-mode").value,
      byId("edit-output-fps").value,
      byId("edit-output-profile").value,
      byId("edit-output-quality").value,
      collectCreativePlan(),
    );
    renderEditProposal(result);
    displayToast("Propuesta preparada localmente. No se inició ningún servicio externo.");
  } catch (error) {
    summary.hidden = false;
    summary.textContent = `No se pudo preparar la propuesta: ${error.message}`;
    byId("render-approval").hidden = true;
    displayToast(`No se pudo preparar la propuesta: ${error.message}`, true);
  } finally {
    button.textContent = "Preparar propuesta local";
    updateProcessButton();
  }
});

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
  const usesOpenAI = proposal.api_plan?.openai_transcription === true;
  const providerSummary = usesOpenAI
    ? "OpenAI para transcribir y Shotstack Stage para un único render."
    : "Shotstack Stage para un único render; no se llamará a OpenAI.";
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
    const result = await startProcessing(proposal.proposal_id, true, unknown);
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
    button.querySelector("span").textContent = "Aprobar y renderizar en Stage";
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
  if (!clientId) throw new Error("Falta configurar GOOGLE_DRIVE_CLIENT_ID en Replit Secrets.");
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
    throw new Error("Configura primero las credenciales de Google Picker en Replit Secrets.");
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

byId("drive-sync-button").addEventListener("click", async () => {
  const button = byId("drive-sync-button");
  button.disabled = true;
  byId("drive-message").classList.remove("error");
  byId("drive-message").textContent = "Descargando solo los archivos elegidos en Picker…";
  try {
    const result = await postDrive(API.driveSync);
    state.library = await loadLibrary();
    state.libraryLoaded = true;
    if (!state.library.some((item) => String(item.id) === String(state.selectedId))) {
      state.selectedId = state.library[0]?.id ?? null;
    }
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

initializePanel();
void initializeMasterPreview();
