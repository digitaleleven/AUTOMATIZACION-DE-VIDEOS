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
