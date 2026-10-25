import "@fontsource/montserrat/800.css";
import { CameraMotionBlur } from "@remotion/motion-blur";
import { useMemo } from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
} from "remotion";
import type { CSSProperties, FC } from "react";
import {
  activeCaptionGroup,
  activeWordIndex,
  groupTimedWords,
} from "./caption-timing.mjs";
import type { TimedWord, CaptionGroup } from "./caption-timing.mjs";

export type CaptionStyle = {
  fontFamily: string;
  highlightColor: string;
  textColor: string;
  fontSize?: number;
  position?: "bottom" | "middle" | "top";
  maxWordsPerCaption?: number;
};

const DEFAULT_CAPTION_STYLE: CaptionStyle = {
  fontFamily: "Montserrat",
  highlightColor: "#FFC0CB",
  textColor: "#202124",
};

const captionContainerStyle: CSSProperties = {
  position: "absolute",
  left: "7%",
  right: "7%",
  top: "75%",
  transform: "translateY(-50%)",
  display: "flex",
  flexWrap: "wrap",
  justifyContent: "center",
  columnGap: "0.28em",
  rowGap: "0.08em",
  color: DEFAULT_CAPTION_STYLE.textColor,
  fontFamily: `${DEFAULT_CAPTION_STYLE.fontFamily}, Arial, sans-serif`,
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
  captionStyle?: CaptionStyle;
  captionGroups?: CaptionGroup[];
}> = ({
  words,
  fps,
  showSubtitles = true,
  captionStyle = DEFAULT_CAPTION_STYLE,
  captionGroups,
}) => {
  const frame = useCurrentFrame();
  const maxWords = [1, 2, 3].includes(captionStyle.maxWordsPerCaption ?? 2)
    ? captionStyle.maxWordsPerCaption ?? 2
    : 2;
  const groups = useMemo(() => captionGroups ?? groupTimedWords(words, 1, maxWords), [words, captionGroups, maxWords]);
  const seconds = frame / fps;
  const group = activeCaptionGroup(groups, seconds);
  if (!showSubtitles || !group) return null;

  const fontFamily = /^[A-Za-z0-9 ,'"-]{1,80}$/u.test(captionStyle.fontFamily)
    ? captionStyle.fontFamily
    : DEFAULT_CAPTION_STYLE.fontFamily;
  const highlightColor = /^#[\da-f]{6}$/iu.test(captionStyle.highlightColor)
    ? captionStyle.highlightColor
    : DEFAULT_CAPTION_STYLE.highlightColor;
  const textColor = /^#[\da-f]{6}$/iu.test(captionStyle.textColor)
    ? captionStyle.textColor
    : DEFAULT_CAPTION_STYLE.textColor;
  const activeIndex = activeWordIndex(group, seconds);
  return (
    <CameraMotionBlur shutterAngle={90} samples={5}>
      <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
        <div style={{
          ...captionContainerStyle,
          color: textColor,
          fontFamily: `${fontFamily}, Arial, sans-serif`,
          fontSize:
            Number.isInteger(captionStyle.fontSize) &&
            (captionStyle.fontSize as number) >= 32 &&
            (captionStyle.fontSize as number) <= 120
              ? captionStyle.fontSize
              : captionContainerStyle.fontSize,
          top:
            captionStyle.position === "top"
              ? "28%"
              : captionStyle.position === "middle"
                ? "50%"
                : "75%",
        }}>
          {group.words.map((word, index) => {
            const isActive = index === activeIndex;
            const wordStartFrame = Math.round(word.start * fps);
            const bounce = isActive
              ? spring({
                  frame: Math.max(0, frame - wordStartFrame),
                  fps,
                  config: { damping: 12, stiffness: 250, mass: 0.55 },
                })
              : 0;
            const popScale = isActive ? 0.9 + bounce * 0.2 : 1;
            return (
              <span
                key={`${word.start.toFixed(4)}-${index}`}
                style={{
                  display: "inline-block",
                  color: isActive ? textColor : "#FFFFFF",
                  WebkitTextStroke: isActive ? "0px transparent" : "1.5px #101010",
                  textShadow: isActive ? "0 1px 2px rgba(0,0,0,0.15)" : "0 3px 8px #101010",
                  backgroundColor: isActive ? highlightColor : "transparent",
                  border: isActive
                    ? "1px solid rgba(20,20,20,0.7)"
                    : "1px solid transparent",
                  borderRadius: 10,
                  padding: "0.16em 0.34em",
                  boxShadow: isActive
                    ? "0 0 0 1px rgba(255,255,255,0.72), 0 5px 14px rgba(0,0,0,0.38), 0 0 12px rgba(255,192,203,0.48)"
                    : undefined,
                  filter: isActive
                    ? "drop-shadow(0 4px 10px rgba(0,0,0,0.38))"
                    : undefined,
                  transform: `scale(${popScale})`,
                  transformOrigin: "center center",
                }}
              >
                {word.text}
              </span>
            );
          })}
        </div>
      </AbsoluteFill>
    </CameraMotionBlur>
  );
};
