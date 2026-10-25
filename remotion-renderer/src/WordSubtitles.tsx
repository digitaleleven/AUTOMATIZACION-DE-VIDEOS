import "@fontsource/montserrat/800.css";
import { useMemo } from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
} from "remotion";
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
  top: "72%",
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
  textTransform: "uppercase",
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
          (() => {
            const isActive = index === activeIndex;
            const wordStartFrame = Math.round(word.start * fps);
            const popScale = isActive
              ? interpolate(
                  frame,
                  [wordStartFrame, wordStartFrame + 5],
                  [0.92, 1],
                  {
                    easing: Easing.out(Easing.cubic),
                    extrapolateLeft: "clamp",
                    extrapolateRight: "clamp",
                  },
                )
              : 1;
            return (
              <span
                key={`${word.start.toFixed(4)}-${index}`}
                style={{
                  display: "inline-block",
                  color: isActive ? ACTIVE_WORD_COLOR : CAPTION_COLOR,
                  backgroundColor: isActive
                    ? ACTIVE_WORD_BACKGROUND
                    : "transparent",
                  borderRadius: 8,
                  padding: "0.12em 0.28em",
                  filter: isActive
                    ? "drop-shadow(0 3px 8px rgba(0,0,0,0.32))"
                    : undefined,
                  transform: `scale(${popScale})`,
                  transformOrigin: "center center",
                }}
              >
                {word.text}
              </span>
            );
          })()
        ))}
      </div>
    </AbsoluteFill>
  );
};
