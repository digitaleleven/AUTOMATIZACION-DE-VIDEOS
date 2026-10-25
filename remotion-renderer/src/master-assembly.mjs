import { groupTimedWords } from "./caption-timing.mjs";

const normalizeWord = (value) =>
  String(value ?? "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, "");

const secondsAtFrame = (frame, fps) => (frame / fps).toFixed(9);

export function buildNaturalReframeFrames(wordTimings, fps = 30, pauseSeconds = 0.65) {
  if (!Array.isArray(wordTimings) || !Number.isInteger(fps) || fps <= 0) {
    throw new Error("Las marcas de palabra para el reencuadre no son válidas.");
  }
  const frames = [];
  let previous = null;
  for (const word of wordTimings) {
    const start = Number(word?.start);
    const end = Number(word?.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) continue;
    const startsPhrase =
      !previous ||
      start - previous.end >= pauseSeconds ||
      /[.!?;:]$/u.test(String(previous.text ?? "").trim());
    if (startsPhrase) frames.push(Math.round(start * fps));
    previous = { start, end, text: word.text };
  }
  return [...new Set(frames)].sort((left, right) => left - right);
}

export function buildJumpFrames(clips, wordTimings, effectCues, fps = 30, overlays = []) {
  if (!Array.isArray(clips) || !Array.isArray(wordTimings) || !Array.isArray(effectCues)) {
    throw new Error("Los datos para los cortes de énfasis no son válidos.");
  }
  const frames = [
    ...clips.slice(1).map((clip) => Number(clip.outputStart)),
    ...buildNaturalReframeFrames(wordTimings, fps),
    ...overlays.map((cue) => cue.startFrame),
  ];
  for (const cue of effectCues) {
    const trigger = normalizeWord(cue.trigger_word);
    if (!trigger) continue;
    const word = wordTimings.find((item) => normalizeWord(item.text) === trigger);
    if (word) frames.push(Math.round(word.start * fps));
  }
  return [...new Set(frames)]
    .filter((frame) => Number.isInteger(frame) && frame >= 0)
    .sort((left, right) => left - right);
}

export function buildSoundEffectCues(
  clips,
  effects,
  wordTimings,
  effectCues,
  fps = 30,
  { includeCaptionEffects = true, overlays = [], captionGroups } = {},
) {
  const cues = [];
  const add = (kind, startFrame, durationFrames) => {
    if (!Number.isInteger(startFrame) || startFrame < 0) return;
    cues.push({ kind, startFrame, durationFrames });
  };

  for (const clip of clips.slice(1)) add("swoosh", clip.outputStart, 12);
  for (const effect of effects) add("pop", effect.startFrame, 8);
  for (const cue of overlays) add("pop", cue.startFrame, 8);
  if (includeCaptionEffects) {
    const groups = captionGroups ?? groupTimedWords(wordTimings, 1, 3);
    for (const group of groups) add("pop", Math.ceil(group.start * fps), 8);
  }

  const unique = new Map();
  for (const cue of cues) unique.set(`${cue.kind}:${cue.startFrame}`, cue);
  return [...unique.values()].sort(
    (left, right) => left.startFrame - right.startFrame || left.kind.localeCompare(right.kind),
  );
}

export function buildZoompanFilter(
  jumpFrames,
  {
    width = 1080,
    height = 1920,
    fps = 30,
    hookDurationFrames = 0,
    easingFrames = 12,
    hookScale = 1.06,
    reframeScale = 1.06,
  } = {},
) {
  if (!Array.isArray(jumpFrames) || !jumpFrames.every((frame) => Number.isInteger(frame) && frame >= 0)) {
    throw new Error("Los fotogramas de énfasis deben ser enteros no negativos.");
  }
  if (
    !Number.isInteger(width) || width <= 0 ||
    !Number.isInteger(height) || height <= 0 ||
    !Number.isInteger(fps) || fps <= 0 ||
    !Number.isInteger(hookDurationFrames) || hookDurationFrames < 0 ||
    !Number.isInteger(easingFrames) || easingFrames <= 0 ||
    !Number.isFinite(hookScale) || hookScale < 1 || hookScale > 1.06 ||
    !Number.isFinite(reframeScale) || reframeScale < 1 || reframeScale > 1.06
  ) {
    throw new Error("La resolución y tasa de fotogramas deben ser enteras y positivas.");
  }
  const reframePulses = [...new Set(jumpFrames)]
    .filter((frame) => frame > hookDurationFrames + easingFrames)
    .map((frame) => {
      const riseEnd = frame + easingFrames;
      const fallEnd = riseEnd + easingFrames;
      const smooth = (start) => {
        const t = `((on-${start})/${easingFrames})`;
        return `(${t}*${t}*(3-2*${t}))`;
      };
      return `if(between(on\\,${frame}\\,${riseEnd})\\,1+${(reframeScale - 1).toFixed(4)}*${smooth(frame)}` +
        `\\,if(between(on\\,${riseEnd}\\,${fallEnd})\\,${reframeScale.toFixed(4)}-${(reframeScale - 1).toFixed(4)}*${smooth(riseEnd)}\\,1))`;
    });
  const reframeExpression = reframePulses.reduce(
    (expression, pulse) => `max(${expression}\\,${pulse})`,
    "1",
  );
  const hookFadeEnd = hookDurationFrames + easingFrames;
  const zoomExpression = hookDurationFrames > 0
    ? `if(between(on\\,0\\,${hookDurationFrames - 1})\\,${hookScale.toFixed(4)}` +
      `\\,if(between(on\\,${hookDurationFrames}\\,${hookFadeEnd})\\,${hookScale.toFixed(4)}-` +
      `${(hookScale - 1).toFixed(4)}*(on-${hookDurationFrames})/${easingFrames}\\,${reframeExpression}))`
    : reframeExpression;
  return `zoompan=z='${zoomExpression}':` +
    `x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=${width}x${height}:fps=${fps}`;
}

export function buildMasterFilterComplex({
  clips,
  sourceAudioStreamIndex,
  colorFilter,
  jumpFrames,
  includeSoundEffects = true,
  width = 1080,
  height = 1920,
  fps = 30,
}) {
  if (!Array.isArray(clips) || clips.length === 0) {
    throw new Error("El montaje debe conservar al menos un segmento.");
  }
  if (!Number.isInteger(sourceAudioStreamIndex) || sourceAudioStreamIndex < 0) {
    throw new Error("No se ha seleccionado una pista AAC estéreo principal válida.");
  }
  if (typeof colorFilter !== "string" || !colorFilter.trim()) {
    throw new Error("Falta el filtro de conversión de color.");
  }

  const graph = [];
  let previousEnd = 0;
  let outputStart = 0;
  for (const clip of clips) {
    if (
      !Number.isInteger(clip.sourceStart) || clip.sourceStart < previousEnd ||
      !Number.isInteger(clip.frames) || clip.frames <= 0 ||
      clip.outputStart !== outputStart
    ) throw new Error("Los segmentos deben ser cronológicos, contiguos en salida y no solaparse.");
    previousEnd = clip.sourceStart + clip.frames;
    outputStart += clip.frames;
  }
  // One decoded video stream, not N split/concat branches buffering full 4K frames.
  const selection = clips.map(
    (clip) => `between(n\\,${clip.sourceStart}\\,${clip.sourceStart + clip.frames - 1})`,
  ).join("+");
  graph.push(`[0:v]fps=${fps},select='${selection}',setpts=N/(${fps}*TB)[vcut]`);
  const colorStages = colorFilter.replace(/^fps=\d+,/u, "");
  graph.push(`[vcut]${colorStages}[vcolor]`);
  graph.push(`[vcolor]${buildZoompanFilter(jumpFrames, { width, height, fps })}[vzoom]`);
  graph.push("[1:v]format=rgba[graphics]");
  graph.push("[vzoom][graphics]overlay=0:0:shortest=1:eof_action=pass:format=auto,format=yuv420p[vout]");

  const audioSource = `0:${sourceAudioStreamIndex}`;
  const audioSplitLabels = clips.map((_, index) => `asrc${index}`);
  if (clips.length === 1) {
    graph.push(`[${audioSource}]anull[${audioSplitLabels[0]}]`);
  } else {
    graph.push(
      `[${audioSource}]asplit=${clips.length}${audioSplitLabels.map((label) => `[${label}]`).join("")}`,
    );
  }
  const audioLabels = clips.map((clip, index) => {
    const label = `aclip${index}`;
    const start = secondsAtFrame(clip.sourceStart, fps);
    const end = secondsAtFrame(clip.sourceStart + clip.frames, fps);
    graph.push(
      `[${audioSplitLabels[index]}]atrim=start=${start}:end=${end},asetpts=PTS-STARTPTS[${label}]`,
    );
    return `[${label}]`;
  });
  graph.push(
    `${audioLabels.join("")}concat=n=${clips.length}:v=0:a=1,aresample=48000,asetpts=PTS-STARTPTS[aoriginal]`,
  );
  if (includeSoundEffects) {
    graph.push("[1:a:0]aresample=48000,asetpts=PTS-STARTPTS[asfx]");
    graph.push(
      "[aoriginal][asfx]amix=inputs=2:duration=first:dropout_transition=0:normalize=0," +
      "alimiter=limit=0.95[aout]",
    );
  } else {
    graph.push("[aoriginal]anull[aout]");
  }
  return graph.join(";");
}
