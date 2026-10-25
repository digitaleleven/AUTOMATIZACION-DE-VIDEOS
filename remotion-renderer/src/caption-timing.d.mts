export interface TimedWord {
  text: string;
  start: number;
  end: number;
  highlight?: boolean;
}

export interface CaptionGroup {
  words: TimedWord[];
  start: number;
  end: number;
}

export function parseWhisperWordTimings(payload: unknown): TimedWord[];
export function mapWordTimingsToOutput(
  words: readonly TimedWord[],
  segments: readonly { sourceStart: number; frames: number; outputStart: number }[],
  fps?: number,
): TimedWord[];
export function isKeywordWord(text: string): boolean;
export function keywordWindowsForSegment(
  words: readonly TimedWord[],
  segment: { outputStart: number; frames: number },
  fps?: number,
): { startFrame: number; endFrame: number }[];
export function groupTimedWords(
  words: readonly TimedWord[],
  minWords?: number,
  maxWords?: number,
): CaptionGroup[];
export function activeCaptionGroup(
  groups: readonly CaptionGroup[],
  seconds: number,
): CaptionGroup | null;
export function activeWordIndex(group: CaptionGroup, seconds: number): number;
