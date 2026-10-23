const API = {
  health: "/api/health",
  library: "/api/library",
  upload: "/api/upload",
  jobs: "/api/jobs",
  style: "/api/style",
};

const state = {
  library: [],
  libraryLoaded: false,
  selectedId: null,
  health: null,
  style: null,
  activeJob: null,
  pollTimer: null,
  styleLoaded: false,
};

const $ = (selector) => document.querySelector(selector);
const byId = (id) => document.getElementById(id);

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, credentials: "same-origin" });
  } catch (error) {
    throw new Error(`No se pudo conectar con el servicio local. ${error.message}`);
  }
  const text = await response.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = { detail: text }; }
  }
  if (!response.ok) {
    const message = body?.detail || body?.error || body?.message || `Error HTTP ${response.status}`;
    throw new Error(String(message));
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

async function startProcessing(sourceId, authorizePaidApis, unknownCostAcknowledged) {
  return requestJson(API.jobs, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_id: sourceId,
      authorize_paid_apis: authorizePaidApis,
      unknown_cost_acknowledged: unknownCostAcknowledged,
    }),
  });
}

async function loadJob(jobId) {
  return requestJson(`${API.jobs}/${encodeURIComponent(jobId)}`);
}

async function loadStyle() {
  return requestJson(API.style);
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
    queued: "En cola", pending: "Pendiente", ready: "Listo", validating: "Validando",
    processing: "Procesando", transcribing: "Transcribiendo", rendering: "Renderizando",
    downloading: "Descargando", completed: "Completado", complete: "Completado",
    failed: "Error", error: "Error", rejected: "Rechazado",
    normalizing: "Preparando compatibilidad", preparing_captions: "Preparando subtítulos",
    uploading: "Subiendo fuente a Shotstack Stage",
    uploading_captions: "Subiendo subtítulos a Shotstack Stage",
    validating_output: "Validando resultado", interrupted: "Interrumpido",
  };
  return labels[String(status || "").toLowerCase()] || (status ? String(status) : "No disponible");
}

function renderLibrary() {
  const list = byId("library-list");
  byId("library-count").textContent = state.library.length ? `${state.library.length} ARCHIVO${state.library.length === 1 ? "" : "S"}` : "0 ARCHIVOS";
  renderSummary();
  if (!state.library.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-glyph">▧</div><h3>Tu mesa está vacía</h3><p>${state.libraryLoaded ? "No hay vídeos en el servicio local. Añade un MP4 o MOV para empezar." : "Consulta la biblioteca local o añade un vídeo MP4 o MOV."}</p><button class="outline-button" type="button" data-action="${state.libraryLoaded ? "upload" : "connect"}">${state.libraryLoaded ? "Añadir un vídeo" : "Cargar biblioteca"}</button></div>`;
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
        <span class="library-item-top"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><span class="file-type">${escapeHtml(item.format || "VIDEO")}</span></span>
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
  byId("summary-source").textContent = "Datos del servicio local";
}

function renderSelectedItem() {
  const item = getSelectedItem();
  const video = byId("source-video");
  const placeholder = $(".stage-placeholder");
  if (!item) {
    video.hidden = true;
    video.removeAttribute("src");
    placeholder.hidden = false;
    hideOutput();
    byId("selected-status").textContent = "NINGÚN ARCHIVO";
    byId("file-summary").innerHTML = `<div class="summary-empty">Los metadatos del archivo aparecerán aquí cuando selecciones una fuente.</div>`;
    byId("cost-value").textContent = "Desconocido";
    byId("cost-note").textContent = "Selecciona un vídeo para consultar la estimación disponible.";
    byId("unknown-cost-wrap").hidden = true;
    updateProcessButton();
    return;
  }
  video.src = sourceMediaUrl(item.id);
  video.load();
  video.hidden = false;
  placeholder.hidden = true;
  byId("selected-status").textContent = statusLabel(item.status).toLocaleUpperCase("es");
  const fields = [
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
  const costKnown = item.cost_estimate_usd !== null && item.cost_estimate_usd !== undefined;
  byId("cost-value").textContent = formatCost(item.cost_estimate_usd);
  byId("cost-note").textContent = costKnown ? "Estimación informada por el servicio; el importe final puede variar." : "El servicio no proporciona una estimación. No se ha supuesto ningún precio.";
  byId("unknown-cost-wrap").hidden = costKnown;
  if (costKnown) byId("unknown-cost-consent").checked = false;
  const itemOutput = item.output_id ?? item.output?.id ?? (typeof item.output === "string" ? item.output : null);
  if (itemOutput) showOutput(itemOutput);
  else hideOutput();
  if (item.error) {
    byId("file-summary").insertAdjacentHTML("beforeend", `<div class="job-error">${escapeHtml(item.error)}</div>`);
  }
  updateProcessButton();
}

function renderHealth(health) {
  state.health = health;
  const set = (id, configured) => {
    const el = byId(id);
    el.textContent = configured === true ? "Presente · no verificada" : configured === false ? "No configurada" : "Desconocido";
    el.className = configured === true ? "health-ok" : configured === false ? "health-no" : "health-unknown";
  };
  set("health-openai", health?.openai_key_configured);
  set("health-shotstack", health?.shotstack_key_configured);
  const env = health?.shotstack_environment;
  byId("health-environment").textContent = env == null || env === "" ? "Desconocido" : String(env);
  const drive = health?.drive_connected;
  byId("health-drive").textContent = drive === true ? "Conectado" : drive === false ? "No conectado" : "Desconocido";
  byId("health-drive").className = drive === true ? "health-ok" : drive === false ? "health-muted" : "health-unknown";
  const connected = Boolean(health);
  byId("connection-label").textContent = connected ? "Servicio conectado" : "Sin conectar";
  $(".local-indicator").classList.toggle("connected", connected);
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
  const progressText = Number.isFinite(progress) ? `${Math.max(0, Math.min(100, progress))}%` : "Progreso no cuantificado";
  const progressMarkup = Number.isFinite(progress)
    ? `<div class="job-progress-track" aria-label="${escapeHtml(progressText)}"><div class="job-progress-fill" style="width:${Math.max(0, Math.min(100, progress))}%"></div></div>`
    : `<div class="job-progress-unmeasured">El servicio informa la fase, no un porcentaje.</div>`;
  const phase = job.phase ? statusLabel(job.phase) : statusLabel(job.status);
  byId("job-status-tag").textContent = statusLabel(job.status).toLocaleUpperCase("es");
  byId("job-content").innerHTML = `<div class="job-live">
    <div><strong>${escapeHtml(sourceName || "Trabajo de procesamiento")}</strong><p>${escapeHtml(phase)} · ID ${escapeHtml(job.id ?? "No disponible")}</p>${progressMarkup}</div>
    <span class="job-progress-label">${escapeHtml(progressText)}</span>
    ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}
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

function updateProcessButton() {
  const item = getSelectedItem();
  const costUnknown = !item || item.cost_estimate_usd === null || item.cost_estimate_usd === undefined;
  const consentOk = byId("paid-consent").checked && (!costUnknown || byId("unknown-cost-consent").checked);
  const busy = state.activeJob && !["completed", "complete", "failed", "error", "rejected"].includes(String(state.activeJob.status || "").toLowerCase());
  const itemStatus = String(item?.status || "").toLowerCase();
  const alreadyProcessed = ["completed", "processing", "queued"].includes(itemStatus);
  byId("process-button").disabled = !item || !consentOk || Boolean(busy) || alreadyProcessed;
  byId("process-hint").textContent = !item ? "Selecciona una fuente antes de continuar." : alreadyProcessed ? (itemStatus === "completed" ? "Este archivo ya tiene un resultado registrado." : "Este archivo ya está en procesamiento.") : !byId("paid-consent").checked ? "Marca la autorización explícita para continuar." : costUnknown && !byId("unknown-cost-consent").checked ? "Confirma también que aceptas el coste desconocido." : busy ? "Hay un trabajo en curso." : "Se enviará el vídeo a OpenAI y Shotstack Stage.";
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
    } else if (field.type === "color") {
      field.value = /^#[0-9a-f]{6}$/i.test(value) ? value : "#000000";
    } else field.value = value == null ? "" : value;
  }
  byId("style-fields").disabled = false;
  byId("style-fields").hidden = false;
  byId("save-style-button").disabled = false;
  byId("style-message").textContent = "Configuración real cargada. Puedes editarla y guardar los cambios.";
}

function collectStyle() {
  const data = {};
  const fields = byId("style-form").elements;
  for (const key of ["font_family", "font_size", "font_color", "highlight_color", "stroke_color", "stroke_width", "animation_style", "caption_position", "caption_offset_y", "emoji_by_keyword", "logo_path", "logo_position", "logo_scale", "logo_offset_x", "logo_offset_y", "output_fps", "max_input_size_mb", "max_video_duration_seconds", "max_transcription_chunks", "openai_price_per_audio_minute_usd", "shotstack_price_per_render_minute_usd", "max_estimated_cost_usd"]) {
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
    } else if (field.type === "number") data[key] = field.value === "" ? null : Number(field.value);
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
  byId("refresh-button").textContent = "Consultando…";
  try {
    const results = await Promise.allSettled([loadHealth(), loadLibrary()]);
    const healthResult = results[0];
    const libraryResult = results[1];
    const errors = [];
    if (healthResult.status === "fulfilled") renderHealth(healthResult.value);
    else errors.push(`Estado del servicio: ${healthResult.reason.message}`);
    if (libraryResult.status === "fulfilled") {
      state.library = libraryResult.value;
      state.libraryLoaded = true;
      if (!state.library.some((item) => String(item.id) === String(state.selectedId))) {
        state.selectedId = state.library[0]?.id ?? null;
      }
      renderLibrary();
      renderSelectedItem();
    } else errors.push(`Biblioteca: ${libraryResult.reason.message}`);
    if (errors.length) displayToast(errors.join(" · "), true);
    else displayToast(state.library.length ? "Estado y biblioteca actualizados." : "Biblioteca vacía: el servicio no tiene vídeos disponibles.");
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
    byId("job-content").insertAdjacentHTML("beforeend", `<div class="job-error">No se pudo consultar el estado del trabajo: ${escapeHtml(error.message)}. Usa «Actualizar datos» para volver a consultar la biblioteca.</div>`);
    displayToast(`Error al consultar el trabajo: ${error.message}`, true);
  }
}

byId("today-label").textContent = new Intl.DateTimeFormat("es", { day: "2-digit", month: "short", year: "numeric" }).format(new Date()).toLocaleUpperCase("es");
byId("connect-button").addEventListener("click", refreshWorkspace);
byId("refresh-button").addEventListener("click", refreshWorkspace);
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
  if (actionButton?.dataset.action === "connect") {
    refreshWorkspace();
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
  message.textContent = `Validando y guardando ${file.name} en el entorno local…`;
  try {
    const uploaded = await uploadVideo(file);
    const returnedId = uploaded?.id ?? uploaded?.item?.id ?? uploaded?.source_id;
    const freshLibrary = await loadLibrary();
    state.library = freshLibrary;
    state.libraryLoaded = true;
    state.selectedId = returnedId ?? freshLibrary.find((item) => item.filename === file.name || item.original_filename === file.name)?.id ?? freshLibrary[0]?.id ?? null;
    renderLibrary();
    renderSelectedItem();
    message.textContent = "Carga local completada. Revisa los metadatos y la estimación antes de procesar.";
    displayToast("Vídeo recibido por el servicio local.");
  } catch (error) {
    message.textContent = `No se pudo cargar el vídeo: ${error.message}`;
    displayToast(`Error de carga: ${error.message}`, true);
  } finally {
    event.target.value = "";
  }
});

byId("paid-consent").addEventListener("change", updateProcessButton);
byId("unknown-cost-consent").addEventListener("change", updateProcessButton);
byId("process-button").addEventListener("click", async () => {
  const item = getSelectedItem();
  if (!item) return;
  const unknown = item.cost_estimate_usd === null || item.cost_estimate_usd === undefined;
  if (!byId("paid-consent").checked || (unknown && !byId("unknown-cost-consent").checked)) {
    displayToast("Falta confirmar todas las autorizaciones requeridas.", true);
    return;
  }
  const button = byId("process-button");
  button.disabled = true;
  button.querySelector("span").textContent = "Iniciando…";
  try {
    const result = await startProcessing(item.id, true, unknown ? true : false);
    const job = result?.job || result;
    state.activeJob = job;
    renderJob(job, item.original_filename || item.filename);
    if (job.id && !["completed", "complete", "failed", "error", "rejected"].includes(String(job.status || "").toLowerCase())) {
      clearInterval(state.pollTimer);
      state.pollTimer = setInterval(() => pollJob(job.id, item.original_filename || item.filename), 2500);
      await pollJob(job.id, item.original_filename || item.filename);
    }
    displayToast("Solicitud de procesamiento enviada con tu autorización.");
  } catch (error) {
    displayToast(`No se pudo iniciar el procesamiento: ${error.message}`, true);
    byId("job-status-tag").textContent = "ERROR AL INICIAR";
    byId("job-content").innerHTML = `<div class="job-error">${escapeHtml(error.message)}</div>`;
  } finally {
    button.querySelector("span").textContent = "Iniciar procesamiento";
    updateProcessButton();
  }
});

byId("load-style-button").addEventListener("click", async () => {
  const button = byId("load-style-button");
  button.disabled = true;
  byId("style-message").textContent = "Consultando la configuración real…";
  try {
    renderStyle(await loadStyle());
  } catch (error) {
    byId("style-message").textContent = `No se pudo cargar el estilo: ${error.message}`;
    displayToast(`Error al cargar el estilo: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});
byId("style-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.styleLoaded) {
    displayToast("Carga primero la configuración real antes de guardar.", true);
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
    byId("style-message").textContent = "Cambios guardados en la configuración del servicio.";
    displayToast("Estilo visual guardado.");
  } catch (error) {
    byId("style-message").textContent = `No se pudieron guardar los cambios: ${error.message}`;
    displayToast(`Error al guardar el estilo: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});
