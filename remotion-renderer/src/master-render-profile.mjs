export const MASTER_OVERLAY_PROFILE = Object.freeze({
  codec: "prores",
  proResProfile: "4444",
  pixelFormat: "yuva444p10le",
  imageFormat: "png",
  audioCodec: "pcm-16",
  sampleRate: 48000,
  concurrency: 1,
  maxRetries: 0,
  disallowParallelEncoding: true,
});

export const SUPPORTED_MASTER_OUTPUT_PROFILES = Object.freeze([
  Object.freeze({ width: 720, height: 1280 }),
  Object.freeze({ width: 1080, height: 1920 }),
  Object.freeze({ width: 2160, height: 3840 }),
]);
export const SUPPORTED_MASTER_FPS = Object.freeze([25, 30, 50, 60]);

export function resolveMasterRenderProfile(width = 1080, height = 1920, fps = 30) {
  const profile = {
    width: Number(width),
    height: Number(height),
    fps: Number(fps),
  };
  if (
    !SUPPORTED_MASTER_OUTPUT_PROFILES.some(
      (item) => item.width === profile.width && item.height === profile.height,
    )
  ) {
    throw new Error("La salida debe ser vertical 720p, 1080p o 2160p.");
  }
  if (!Number.isInteger(profile.fps) || !SUPPORTED_MASTER_FPS.includes(profile.fps)) {
    throw new Error("La tasa de salida debe ser 25, 30, 50 o 60 FPS.");
  }
  return Object.freeze(profile);
}

export const SUPPORTED_ALPHA_OVERLAY_PIXEL_FORMATS = Object.freeze([
  "yuva444p10le",
  "yuva444p12le",
]);

export const supportsAlphaOverlayPixelFormat = (pixelFormat) =>
  SUPPORTED_ALPHA_OVERLAY_PIXEL_FORMATS.includes(pixelFormat);

export const resolveRenderFrameCount = (requested, durationInFrames) => {
  if (!Number.isInteger(durationInFrames) || durationInFrames <= 0) {
    throw new Error("La duración de la composición debe ser un número positivo de fotogramas.");
  }
  if (requested === undefined || requested === null) return durationInFrames;
  const frameCount = Number(requested);
  if (
    !Number.isInteger(frameCount) ||
    frameCount <= 0 ||
    frameCount > durationInFrames
  ) {
    throw new Error(
      `--render-frames debe ser un entero entre 1 y ${durationInFrames}.`,
    );
  }
  return frameCount;
};
