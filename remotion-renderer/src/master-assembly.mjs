const normalizeWord = (value) =>
  String(value ?? "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, "");

const secondsAtFrame = (frame, fps) => (frame / fps).toFixed(9);

export function buildJumpFrames(clips, wordTimings, effectCues, fps = 30) {
  if (!Array.isArray(clips) || !Array.isArray(wordTimings) || !Array.isArray(effectCues)) {
    throw new Error("Los datos para los cortes de énfasis no son válidos.");
  }
  const frames = clips.slice(1).map((clip) => Number(clip.outputStart));
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

export function buildSoundEffectCues(clips, effects, wordTimings, effectCues, fps = 30) {
  const cues = [];
  const add = (kind, startFrame, durationFrames) => {
    if (!Number.isInteger(startFrame) || startFrame < 0) return;
    cues.push({ kind, startFrame, durationFrames });
  };

  for (const clip of clips.slice(1)) add("swoosh", clip.outputStart, 12);
  for (const effect of effects) add("pop", effect.startFrame, 8);
  for (const cue of effectCues) {
    const trigger = normalizeWord(cue.trigger_word);
    const word = wordTimings.find((item) => normalizeWord(item.text) === trigger);
    if (word) add("ding", Math.round(word.start * fps), 10);
  }

  const unique = new Map();
  for (const cue of cues) unique.set(`${cue.kind}:${cue.startFrame}`, cue);
  return [...unique.values()].sort(
    (left, right) => left.startFrame - right.startFrame || left.kind.localeCompare(right.kind),
  );
}

export function buildZoompanFilter(jumpFrames, { width = 1080, height = 1920, fps = 30 } = {}) {
  if (!Array.isArray(jumpFrames) || !jumpFrames.every((frame) => Number.isInteger(frame) && frame >= 0)) {
    throw new Error("Los fotogramas de énfasis deben ser enteros no negativos.");
  }
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0 || !Number.isInteger(fps) || fps <= 0) {
    throw new Error("La resolución y tasa de fotogramas deben ser enteras y positivas.");
  }
  if (jumpFrames.length === 0) return "null";
  const condition = [...new Set(jumpFrames)]
    .map((frame) => `eq(on\\,${frame})`)
    .join("+");
  return `zoompan=z='if(${condition}\\,1.06\\,1)':` +
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
  const videoLabels = clips.map((_, index) => `vsrc${index}`);
  if (clips.length === 1) {
    graph.push(`[0:v]fps=${fps}[${videoLabels[0]}]`);
  } else {
    graph.push(`[0:v]fps=${fps},split=${clips.length}${videoLabels.map((label) => `[${label}]`).join("")}`);
  }
  const clipLabels = clips.map((clip, index) => {
    const label = `vclip${index}`;
    graph.push(
      `[${videoLabels[index]}]trim=start_frame=${clip.sourceStart}:end_frame=${clip.sourceStart + clip.frames},` +
      `setpts=PTS-STARTPTS[${label}]`,
    );
    return `[${label}]`;
  });
  graph.push(`${clipLabels.join("")}concat=n=${clips.length}:v=1:a=0[vcut]`);
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
