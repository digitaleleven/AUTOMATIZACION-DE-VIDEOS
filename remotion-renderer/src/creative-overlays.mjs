const normalizeToken = (value) =>
  String(value ?? "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase("es")
    .replace(/[^\p{L}\p{N}]/gu, "");

const POSITIONS = new Set([
  "topLeft",
  "topRight",
  "left",
  "right",
  "bottomLeft",
  "bottomRight",
]);

const safeText = (value, name, maxLength = 120) => {
  const text = String(value ?? "").trim();
  if (!text || text.length > maxLength || /[\u0000-\u001f\u007f]/u.test(text)) {
    throw new Error(`${name} debe tener entre 1 y ${maxLength} caracteres imprimibles.`);
  }
  return text;
};

const safePosition = (value = "right") => {
  if (!POSITIONS.has(value)) {
    throw new Error("La posición del overlay no está permitida.");
  }
  return value;
};

const safeDurationFrames = (seconds, fps, remainingFrames) => {
  const duration = seconds === undefined ? 2.5 : Number(seconds);
  if (!Number.isFinite(duration) || duration < 1.5 || duration > 12) {
    throw new Error("La duración de tarjetas e imágenes debe estar entre 1.5 y 12 segundos.");
  }
  return Math.min(Math.round(duration * fps), remainingFrames);
};

function findTriggerFrame(triggerText, words, fps) {
  const phrase = String(triggerText ?? "")
    .split(/\s+/u)
    .map(normalizeToken)
    .filter(Boolean);
  if (phrase.length === 0) return null;
  for (let index = 0; index <= words.length - phrase.length; index += 1) {
    const matches = phrase.every(
      (token, offset) => normalizeToken(words[index + offset]?.text) === token,
    );
    if (matches) return Math.round(Number(words[index].start) * fps);
  }
  return null;
}

function parseBriefing(briefing) {
  const parsed = { title: null, promo: null, images: [], cards: [] };
  for (const line of String(briefing ?? "").split(/\r?\n/u)) {
    const match = line.match(/^\s*(title|promo|pip|card)\s*[:=]\s*(.*?)\s*$/iu);
    if (!match) continue;
    const [, rawKind, value] = match;
    const kind = rawKind.toLowerCase();
    if (kind === "title" || kind === "promo") {
      parsed[kind] = safeText(value, kind === "title" ? "El título" : "La etiqueta promocional");
      continue;
    }
    const parts = value.split("|").map((part) => part.trim());
    if (kind === "pip") {
      if (parts.length < 3 || parts.length > 4) {
        throw new Error("Usa pip=archivo.png|etiqueta|frase de audio|posición.");
      }
      parsed.images.push({
        asset: parts[0],
        label: parts[1],
        triggerText: parts[2],
        position: parts[3] || "right",
      });
    } else {
      if (parts.length < 2 || parts.length > 3) {
        throw new Error("Usa card=texto|frase de audio|posición.");
      }
      parsed.cards.push({
        text: parts[0],
        triggerText: parts[1],
        position: parts[2] || "left",
      });
    }
  }
  return parsed;
}

export function buildCreativeOverlays({
  briefing = "",
  planOverlays = {},
  wordTimings = [],
  durationInFrames,
  fps = 30,
} = {}) {
  if (
    !planOverlays ||
    typeof planOverlays !== "object" ||
    Array.isArray(planOverlays) ||
    !Array.isArray(wordTimings) ||
    !Number.isInteger(durationInFrames) ||
    durationInFrames <= 0 ||
    !Number.isInteger(fps) ||
    fps <= 0
  ) {
    throw new Error("La configuración de overlays no es válida.");
  }

  const fromBriefing = parseBriefing(briefing);
  const titleSpec = planOverlays.title ?? fromBriefing.title;
  const promoSpec = planOverlays.promo ?? fromBriefing.promo;
  const titleText = titleSpec && typeof titleSpec === "object"
    ? safeText(titleSpec.text, "El título")
    : titleSpec ? safeText(titleSpec, "El título") : null;
  const promoText = promoSpec && typeof promoSpec === "object"
    ? safeText(promoSpec.text, "La etiqueta promocional")
    : promoSpec ? safeText(promoSpec, "La etiqueta promocional") : null;
  const titleDurationSeconds = titleSpec && typeof titleSpec === "object"
    ? titleSpec.duration_seconds
    : undefined;
  const titleDurationFrames = titleText
    ? safeDurationFrames(titleDurationSeconds ?? 3.5, fps, durationInFrames)
    : 0;

  const imageSpecs = [
    ...fromBriefing.images,
    ...(Array.isArray(planOverlays.images) ? planOverlays.images : []),
  ];
  const cardSpecs = [
    ...fromBriefing.cards,
    ...(Array.isArray(planOverlays.cards) ? planOverlays.cards : []),
  ];
  const warnings = [];
  const resolveTimed = (spec, kind, index) => {
    const triggerText = safeText(
      spec.trigger_text ?? spec.triggerText ?? "",
      `La referencia de audio del overlay ${index + 1}`,
    );
    let startFrame = findTriggerFrame(triggerText, wordTimings, fps);
    if (startFrame === null && Number.isFinite(Number(spec.start_seconds))) {
      startFrame = Math.round(Number(spec.start_seconds) * fps);
    }
    if (startFrame === null) {
      warnings.push(`No se encontró «${triggerText}» en la transcripción; se omitió el overlay ${index + 1}.`);
      return null;
    }
    if (!Number.isInteger(startFrame) || startFrame < 0 || startFrame >= durationInFrames) {
      warnings.push(`La señal de audio de ${kind} «${triggerText}» queda fuera del vídeo.`);
      return null;
    }
    const durationFrames = safeDurationFrames(
      spec.duration_seconds ?? spec.durationSeconds,
      fps,
      durationInFrames - startFrame,
    );
    if (durationFrames < Math.round(fps * 0.5)) {
      warnings.push(`No hay tiempo suficiente para mostrar ${kind} «${triggerText}».`);
      return null;
    }
    return {
      startFrame,
      durationFrames,
      position: safePosition(spec.position),
      triggerText,
    };
  };

  const images = imageSpecs.map((spec, index) => {
    if (!spec || typeof spec !== "object") throw new Error("Cada imagen PIP debe ser un objeto.");
    const timing = resolveTimed(spec, "imagen PIP", index);
    if (!timing) return null;
    const asset = safeText(spec.asset, "El nombre del recurso PIP", 120);
    if (!/^[a-z0-9][a-z0-9._-]*\.(?:png|jpe?g|webp)$/iu.test(asset)) {
      throw new Error("Las imágenes PIP deben ser archivos PNG, JPG o WebP locales con nombre simple.");
    }
    return {
      id: `pip-${index}`,
      ...timing,
      asset,
      label: spec.label ? safeText(spec.label, "La etiqueta PIP", 80) : "",
    };
  }).filter(Boolean);

  const cards = cardSpecs.map((spec, index) => {
    if (!spec || typeof spec !== "object") throw new Error("Cada tarjeta debe ser un objeto.");
    const timing = resolveTimed(spec, "tarjeta", index);
    if (!timing) return null;
    return {
      id: `card-${index}`,
      ...timing,
      text: safeText(spec.text, "El texto de tarjeta", 90),
    };
  }).filter(Boolean);

  return {
    title: titleText ? { text: titleText, durationFrames: titleDurationFrames } : null,
    promo: promoText ? { text: promoText } : null,
    images,
    cards,
    warnings,
  };
}
