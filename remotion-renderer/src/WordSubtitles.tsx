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
const ACTIVE_WORD_COLOR = "#111111";
const ACTIVE_WORD_BACKGROUND = "#FFE600";

const captionStyle: CSSProperties = {
  position: "absolute",
  left: "7%",
  right: "7%",
  top: "70%",
  transform: "translateY(-50%)",
  display: "flex",
  flexWrap: "wrap",
  justifyContent: "center",
  columnGap: "0.28em",
  rowGap: "0.08em",
  color: CAPTION_COLOR,
  fontFamily: "Montserrat, Arial, sans-serif",
  fontSize: 54,
  fontWeight: 800,
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
  showSubtitles?: boolean;
}> = ({ words, fps, showSubtitles = true }) => {
  const frame = useCurrentFrame();
  const groups = useMemo(() => groupTimedWords(words, 1, 3), [words]);
  const seconds = frame / fps;
  const group = activeCaptionGroup(groups, seconds);
  if (!showSubtitles || !group) return null;

  const activeIndex = activeWordIndex(group, seconds);
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
      <div style={captionStyle}>
        {group.words.map((word, index) => (
          <span
            key={`${word.start.toFixed(4)}-${index}`}
            style={{
              color: index === activeIndex ? ACTIVE_WORD_COLOR : CAPTION_COLOR,
              backgroundColor: index === activeIndex
                ? ACTIVE_WORD_BACKGROUND
                : "transparent",
              borderRadius: "0.14em",
              padding: "0.06em 0.16em",
            }}
          >
            {word.text}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};
