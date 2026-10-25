export const REMOTION_RENDER_PROFILE = Object.freeze({
  width: 1080,
  height: 1920,
  fps: 50,
  proxyCrf: 14,
  outputCrf: 17,
  x264Preset: "medium",
  pixelFormat: "yuv420p",
  colorSpace: "bt709",
  concurrency: 1,
  maxRetries: 0,
  disallowParallelEncoding: true,
  offthreadVideoThreads: 2,
  offthreadVideoCacheSizeInBytes: 32 * 1024 * 1024,
});

export const hasVisualContent = (lumaSamples, threshold = 40) => {
  const values = lumaSamples.filter(
    (value) => Number.isFinite(value) && value >= 0 && value <= 255,
  );
  if (values.length < 3) return false;
  const visibleSamples = values.filter((value) => value >= threshold).length;
  return visibleSamples >= Math.ceil(values.length / 2);
};

export const parseAllIntraFrameInfo = (ffprobeCsv) => {
  const rows = ffprobeCsv
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => line.split(",").map((value) => value.trim()));
  return {
    frameCount: rows.length,
    allIntra:
      rows.length > 0 &&
      rows.every(([keyFrame, pictureType]) => keyFrame === "1" && pictureType === "I"),
  };
};

export const buildColorManagedProxyFilter = (
  video,
  fps = REMOTION_RENDER_PROFILE.fps,
  { toneMap = "hable", mobiusParam = 0.5 } = {},
) => {
  if (!["hable", "mobius", "direct"].includes(toneMap)) {
    throw new Error(`Curva de mapeo tonal no admitida: ${toneMap}`);
  }
  if (toneMap === "mobius" && (!Number.isFinite(mobiusParam) || mobiusParam <= 0)) {
    throw new Error("El parámetro Mobius debe ser positivo y finito.");
  }

  const rangeIn =
    video.color_range === "tv" ? "limited" :
    video.color_range === "pc" ? "full" :
    null;
  if (!rangeIn) {
    throw new Error("FFprobe no devolvió un rango de color explícito; se cancela para no etiquetar color supuesto.");
  }

  let colorTransform;
  if (
    video.color_transfer === "arib-std-b67" &&
    video.color_primaries === "bt2020" &&
    video.color_space === "bt2020nc"
  ) {
    if (rangeIn !== "limited") {
      throw new Error("La entrada HLG BT.2020 no declara rango limitado; no se aplicará una conversión supuesta.");
    }
    if (toneMap === "direct") {
      colorTransform =
        "zscale=primariesin=bt2020:transferin=arib-std-b67:matrixin=bt2020nc:rangein=limited:" +
        "primaries=bt709:transfer=bt709:matrix=bt709:range=limited:c=topleft";
    } else {
      colorTransform =
        "zscale=primariesin=bt2020:transferin=arib-std-b67:matrixin=bt2020nc:rangein=limited:" +
        "primaries=bt2020:transfer=linear:matrix=gbr:range=full:npl=100," +
        `format=gbrpf32le,tonemap=tonemap=${toneMap}:` +
        (toneMap === "mobius" ? `param=${mobiusParam},` : "desat=0,") +
        "zscale=primariesin=bt2020:transferin=linear:matrixin=gbr:rangein=full:" +
        "primaries=bt709:transfer=bt709:matrix=bt709:range=limited:c=topleft";
    }
  } else if (
    video.color_transfer === "bt709" &&
    video.color_primaries === "bt709" &&
    video.color_space === "bt709"
  ) {
    colorTransform =
      `zscale=primariesin=bt709:transferin=bt709:matrixin=bt709:rangein=${rangeIn}:` +
        "primaries=bt709:transfer=bt709:matrix=bt709:range=limited:c=topleft";
  } else {
    throw new Error(
      `Perfil de entrada no admitido para esta prueba: ${JSON.stringify({
        color_range: video.color_range,
        color_space: video.color_space,
        color_transfer: video.color_transfer,
        color_primaries: video.color_primaries,
      })}`,
    );
  }

  const scaleAndPad =
    toneMap !== "hable"
      ? `scale=${REMOTION_RENDER_PROFILE.width}:${REMOTION_RENDER_PROFILE.height}:` +
        "force_original_aspect_ratio=decrease:flags=lanczos," +
        `pad=${REMOTION_RENDER_PROFILE.width}:${REMOTION_RENDER_PROFILE.height}:(ow-iw)/2:(oh-ih)/2`
      : `scale=${REMOTION_RENDER_PROFILE.width}:${REMOTION_RENDER_PROFILE.height}:` +
        "force_original_aspect_ratio=increase:flags=lanczos," +
        `crop=${REMOTION_RENDER_PROFILE.width}:${REMOTION_RENDER_PROFILE.height}`;

  const directEq =
    toneMap === "direct"
      ? ",eq=brightness=-0.08:contrast=1.05:saturation=0.88:gamma=0.95"
      : "";
  return `${colorTransform},${scaleAndPad}${directEq},setsar=1,fps=${fps},format=${REMOTION_RENDER_PROFILE.pixelFormat}`;
};
