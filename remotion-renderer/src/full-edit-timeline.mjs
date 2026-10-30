export const FULL_FPS = 30;
export const FULL_WIDTH = 1080;
export const FULL_HEIGHT = 1920;
export const FULL_DURATION_IN_FRAMES = 1747;
export const VECTOR_DURATION_IN_FRAMES = 54;

export function cameraScaleAtFrame(
  frame,
  reframeFrames = [],
  easingFrames = 12,
  maxScale = 1.06,
  zoomWindows = [],
) {
  if (!Number.isFinite(frame) || !Array.isArray(reframeFrames)) return 1;
  if (
    !Number.isInteger(easingFrames) || easingFrames <= 0 ||
    !Number.isFinite(maxScale) || maxScale < 1 || maxScale > 1.06 ||
    !Array.isArray(zoomWindows)
  ) {
    throw new Error("La curva de reencuadre no es válida.");
  }
  const smooth = (value) => value * value * (3 - 2 * value);
  let scale = 1;
  for (const start of reframeFrames) {
    if (!Number.isInteger(start) || start <= easingFrames) continue;
    if (frame >= start && frame <= start + easingFrames) {
      const progress = (frame - start) / easingFrames;
      scale = Math.max(scale, 1 + (maxScale - 1) * smooth(progress));
    } else if (frame > start + easingFrames && frame <= start + easingFrames * 2) {
      const progress = (frame - start - easingFrames) / easingFrames;
      scale = Math.max(scale, maxScale - (maxScale - 1) * smooth(progress));
    }
  }
  for (const window of zoomWindows) {
    const startFrame = Number(window?.startFrame);
    const endFrame = Number(window?.endFrame);
    if (
      !Number.isInteger(startFrame) || !Number.isInteger(endFrame) ||
      startFrame < 0 || endFrame <= startFrame
    ) {
      throw new Error("Una ventana de zoom temporizada no es válida.");
    }
    const rampFrames = Math.min(easingFrames, Math.floor((endFrame - startFrame) / 2));
    if (rampFrames <= 0) continue;
    if (window.effect === "zoomOut") {
      const preStartFrame = Math.max(0, startFrame - rampFrames);
      if (frame >= preStartFrame && frame < startFrame) {
        const progress = (frame - preStartFrame) / Math.max(1, startFrame - preStartFrame);
        scale = Math.max(scale, 1 + (maxScale - 1) * smooth(progress));
      } else if (frame >= startFrame && frame <= startFrame + rampFrames) {
        const progress = (frame - startFrame) / rampFrames;
        scale = Math.max(scale, maxScale - (maxScale - 1) * smooth(progress));
      }
      continue;
    }
    if (frame < startFrame || frame > endFrame) continue;
    if (frame < startFrame + rampFrames) {
      const progress = (frame - startFrame) / rampFrames;
      scale = Math.max(scale, 1 + (maxScale - 1) * smooth(progress));
    } else if (frame > endFrame - rampFrames) {
      const progress = (frame - (endFrame - rampFrames)) / rampFrames;
      scale = Math.max(scale, maxScale - (maxScale - 1) * smooth(progress));
    } else {
      scale = Math.max(scale, maxScale);
    }
  }
  return scale;
}

export function isSceneCutBlurFrame(frame, sceneCutFrames) {
  return Number.isInteger(frame) &&
    Array.isArray(sceneCutFrames) &&
    sceneCutFrames.some((cutFrame) => Number.isInteger(cutFrame) && frame === cutFrame);
}

export function buildKeepSegments(sourceFrames, cuts, fps = FULL_FPS) {
  if (!Number.isInteger(sourceFrames) || sourceFrames <= 0) {
    throw new Error("La duración de fuente debe ser un número entero de fotogramas.");
  }
  if (!Number.isFinite(fps) || fps <= 0 || !Array.isArray(cuts)) {
    throw new Error("El plan de cortes no es válido.");
  }

  const ranges = cuts
    .map((cut) => ({
      startFrame: Math.round(Number(cut.start_seconds) * fps),
      endFrame: Math.round(Number(cut.end_seconds) * fps),
    }))
    .sort((left, right) => left.startFrame - right.startFrame);

  let sourceCursor = 0;
  let outputCursor = 0;
  const segments = [];
  for (const range of ranges) {
    if (
      !Number.isInteger(range.startFrame) ||
      !Number.isInteger(range.endFrame) ||
      range.startFrame < sourceCursor ||
      range.endFrame <= range.startFrame ||
      range.endFrame > sourceFrames
    ) {
      throw new Error("Los cortes tienen límites inválidos, solapados o fuera del vídeo.");
    }
    if (range.startFrame > sourceCursor) {
      const frames = range.startFrame - sourceCursor;
      segments.push({
        sourceStart: sourceCursor,
        frames,
        outputStart: outputCursor,
      });
      outputCursor += frames;
    }
    sourceCursor = range.endFrame;
  }

  if (sourceCursor < sourceFrames) {
    const frames = sourceFrames - sourceCursor;
    segments.push({
      sourceStart: sourceCursor,
      frames,
      outputStart: outputCursor,
    });
  }

  if (segments.length === 0) {
    throw new Error("El plan de cortes elimina todos los fotogramas.");
  }
  return segments;
}

export function buildTimedVectorEffects(cues, durationInFrames, fps = FULL_FPS) {
  if (!Array.isArray(cues) || !Number.isInteger(durationInFrames) || durationInFrames <= 0) {
    throw new Error("Las señales de elementos vectoriales no son válidas.");
  }

  return cues.map((cue, index) => {
    const startFrame = Math.round(Number(cue.start_seconds) * fps);
    const requestedDuration = Number(cue.duration_seconds ?? VECTOR_DURATION_IN_FRAMES / fps);
    const requestedFrames = Math.round(requestedDuration * fps);
    const durationFrames = Math.min(requestedFrames, durationInFrames - startFrame);
    const assets = Array.isArray(cue.assets)
      ? cue.assets.map((asset) => `${asset}.svg`)
      : [];
    if (
      !Number.isInteger(startFrame) ||
      startFrame < 0 ||
      !Number.isFinite(requestedDuration) ||
      requestedDuration < 1.5 ||
      requestedDuration > 2.0 ||
      !Number.isInteger(requestedFrames) ||
      durationFrames < Math.round(fps * 1.5) ||
      assets.length === 0 ||
      !assets.every((asset) => /^[a-z0-9-]+\.svg$/u.test(asset))
    ) {
      throw new Error(`La señal vectorial ${index} no cabe en la duración o tiene recursos inválidos.`);
    }
    return {
      id: `${cue.effect}-${index}`,
      startFrame,
      durationFrames,
      assets,
      position: cue.position || "topRight",
    };
  });
}
