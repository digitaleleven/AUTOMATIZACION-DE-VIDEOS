export const FPS = 30;
export const TRANSITION_FRAMES = 12;

export const SEGMENTS = Object.freeze([
  Object.freeze({ sourceStart: 0, frames: 60, outputStart: 0 }),
  Object.freeze({ sourceStart: 150, frames: 75, outputStart: 48 }),
  Object.freeze({ sourceStart: 300, frames: 60, outputStart: 111 }),
]);

export const DURATION_IN_FRAMES =
  SEGMENTS.reduce((sum, segment) => sum + segment.frames, 0) -
  TRANSITION_FRAMES * (SEGMENTS.length - 1);

export const getSourceClipWindow = (sourceStart, frames, sourceDurationInFrames) => {
  if (
    !Number.isInteger(sourceStart) ||
    sourceStart < 0 ||
    !Number.isInteger(frames) ||
    frames <= 0 ||
    !Number.isInteger(sourceDurationInFrames) ||
    sourceDurationInFrames <= 0 ||
    sourceStart + frames > sourceDurationInFrames
  ) {
    throw new Error("Invalid source clip frame range.");
  }

  return {
    trimBefore: sourceStart,
    durationInFrames: frames,
  };
};
