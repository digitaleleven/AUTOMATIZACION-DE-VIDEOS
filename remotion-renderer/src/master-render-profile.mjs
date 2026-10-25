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
