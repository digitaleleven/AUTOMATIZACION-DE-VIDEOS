export const FPS = 50;
export const TRANSITION_FRAMES = 20;

export const SEGMENTS = Object.freeze([
  Object.freeze({ sourceStart: 0, frames: 100, outputStart: 0 }),
  Object.freeze({ sourceStart: 250, frames: 125, outputStart: 80 }),
  Object.freeze({ sourceStart: 500, frames: 100, outputStart: 185 }),
]);

export const DURATION_IN_FRAMES =
  SEGMENTS.reduce((sum, segment) => sum + segment.frames, 0) -
  TRANSITION_FRAMES * (SEGMENTS.length - 1);

export const getSourceTrimWindow = (sourceStart, frames, fps = FPS) => {
  if (
    !Number.isInteger(sourceStart) ||
    sourceStart < 0 ||
    !Number.isInteger(frames) ||
    frames <= 0 ||
    !Number.isFinite(fps) ||
    fps <= 0
  ) {
    throw new Error("Invalid source trim values.");
  }

  return {
    trimBefore: sourceStart / fps,
    trimAfter: (sourceStart + frames) / fps,
  };
};
