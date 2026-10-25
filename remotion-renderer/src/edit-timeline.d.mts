export interface EditSegment {
  sourceStart: number;
  frames: number;
  outputStart: number;
}

export const FPS: number;
export const TRANSITION_FRAMES: number;
export const SEGMENTS: readonly EditSegment[];
export const DURATION_IN_FRAMES: number;
