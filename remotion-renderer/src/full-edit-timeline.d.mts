export type KeepSegment = {
  sourceStart: number;
  frames: number;
  outputStart: number;
};

export type TimedVectorEffect = {
  id: string;
  startFrame: number;
  durationFrames: number;
  assets: string[];
  position: string;
};

export const FULL_FPS: number;
export const FULL_WIDTH: number;
export const FULL_HEIGHT: number;
export const FULL_DURATION_IN_FRAMES: number;
export const VECTOR_DURATION_IN_FRAMES: number;
export function cameraScaleAtFrame(frame: number): number;
export function buildKeepSegments(sourceFrames: number, cuts: Array<{ start_seconds: number; end_seconds: number }>, fps?: number): KeepSegment[];
export function buildTimedVectorEffects(cues: Array<{ effect: string; start_seconds: number; assets: string[]; position?: string }>, durationInFrames: number, fps?: number): TimedVectorEffect[];
