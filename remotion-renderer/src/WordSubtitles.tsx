import { useMemo } from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import type { CSSProperties, FC } from "react";
import {
  activeCaptionGroup,
  activeWordIndex,
  groupTimedWords,
} from "./caption-timing.mjs";
import type { TimedWord } from "./caption-timing.mjs";

const CAPTION_COLOR = "#FFFFFF";
const ACTIVE_WORD_COLOR = "#FFD633";

const captionStyle: CSSProperties = {
  position: "absolute",
  left: "7%",
  right: "7%",
  bottom: "17%",
  display: "flex",
  flexWrap: "wrap",
  justifyContent: "center",
  columnGap: "0.28em",
  rowGap: "0.08em",
  color: CAPTION_COLOR,
  fontFamily: "Arial, sans-serif",
  fontSize: 54,
  fontWeight: 900,
  lineHeight: 1.12,
  textAlign: "center",
  WebkitTextStroke: "1.5px #101010",
  textShadow: "0 3px 8px #101010",
  backgroundColor: "transparent",
  pointerEvents: "none",
};

export const WordSubtitles: FC<{
  words: TimedWord[];
  fps: number;
}> = ({ words, fps }) => {
  const frame = useCurrentFrame();
  const groups = useMemo(() => groupTimedWords(words, 2, 4), [words]);
  const seconds = frame / fps;
  const group = activeCaptionGroup(groups, seconds);
  if (!group) return null;

  const activeIndex = activeWordIndex(group, seconds);
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
      <div style={captionStyle}>
        {group.words.map((word, index) => (
          <span
            key={`${word.start.toFixed(4)}-${index}`}
            style={{
              color: index === activeIndex ? ACTIVE_WORD_COLOR : CAPTION_COLOR,
            }}
          >
            {word.text}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};
