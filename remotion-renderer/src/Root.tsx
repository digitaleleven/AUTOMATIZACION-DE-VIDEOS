import {
  AbsoluteFill,
  Audio,
  Composition,
  Easing,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";
import { useMemo } from "react";
import type { CSSProperties, FC } from "react";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { WordSubtitles } from "./WordSubtitles";
import { keywordWindowsForSegment } from "./caption-timing.mjs";
import type { TimedWord } from "./caption-timing.mjs";
import {
  DURATION_IN_FRAMES,
  FPS,
  SEGMENTS,
  TRANSITION_FRAMES,
  getSourceClipWindow,
} from "./edit-timeline.mjs";

type KeywordWindow = {
  startFrame: number;
  endFrame: number;
};

type ClipProps = {
  sourceFile: string;
  sourceDurationInFrames: number;
  sourceStart: number;
  frames: number;
  keywordWindows: KeywordWindow[];
};

const Clip: FC<ClipProps> = ({
  sourceFile,
  sourceDurationInFrames,
  sourceStart,
  frames,
  keywordWindows,
}) => {
  const frame = useCurrentFrame();
  const sourceClip = getSourceClipWindow(
    sourceStart,
    frames,
    sourceDurationInFrames,
  );
  const scale = keywordWindows.reduce((largestScale, window) => {
    const wordDuration = Math.max(1, window.endFrame - window.startFrame);
    const rampFrames = Math.max(1, Math.min(5, Math.floor(wordDuration / 2)));
    const wordScale = interpolate(
      frame,
      [
        window.startFrame - rampFrames,
        window.startFrame,
        window.endFrame,
        window.endFrame + rampFrames,
      ],
      [1, 1.05, 1.12, 1],
      {
        easing: Easing.inOut(Easing.quad),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      },
    );
    return Math.max(largestScale, wordScale);
  }, 1);

  return (
    <AbsoluteFill style={{ backgroundColor: "#09090b", overflow: "hidden" }}>
      <OffthreadVideo
        src={staticFile(sourceFile)}
        trimBefore={sourceClip.trimBefore}
        durationInFrames={sourceClip.durationInFrames}
        muted
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: `scale(${scale})`,
        }}
      />
    </AbsoluteFill>
  );
};

const audioVolume = (
  frame: number,
  duration: number,
  fadeInFrames: number,
  fadeOutFrames: number,
): number => {
  if (fadeInFrames > 0 && frame < fadeInFrames) {
    return interpolate(frame, [0, fadeInFrames], [0, 1], {
      extrapolateRight: "clamp",
    });
  }

  const fadeOutStart = duration - fadeOutFrames;
  if (fadeOutFrames > 0 && frame >= fadeOutStart) {
    return interpolate(frame, [fadeOutStart, duration], [1, 0], {
      extrapolateRight: "clamp",
    });
  }

  return 1;
};

const AudioSegment: FC<{
  audioFile: string | null;
  sourceStart: number;
  frames: number;
  outputStart: number;
  fadeInFrames: number;
  fadeOutFrames: number;
}> = ({
  audioFile,
  sourceStart,
  frames,
  outputStart,
  fadeInFrames,
  fadeOutFrames,
}) =>
  audioFile ? (
    <Sequence from={outputStart} durationInFrames={frames} layout="none">
      <Audio
        src={staticFile(audioFile)}
        trimBefore={sourceStart}
        durationInFrames={frames}
        volume={(frame) =>
          audioVolume(frame, frames, fadeInFrames, fadeOutFrames)
        }
      />
    </Sequence>
  ) : null;

const STICKER_DURATION_FRAMES = Math.round(FPS * 1.8);

const AnimatedSticker: FC<{
  kind: "drop" | "kiss";
  position: "left" | "right";
}> = ({ kind, position }) => {
  const frame = useCurrentFrame();
  const fadeInFrames = Math.round(FPS * 0.25);
  const fadeOutFrames = Math.round(FPS * 0.2);
  const opacity = Math.min(
    interpolate(frame, [0, fadeInFrames], [0, 0.92], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
    interpolate(
      frame,
      [STICKER_DURATION_FRAMES - fadeOutFrames, STICKER_DURATION_FRAMES],
      [0.92, 0],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    ),
  );
  const scale = interpolate(frame, [0, Math.round(FPS * 0.3)], [0.76, 1], {
    easing: Easing.out(Easing.back(1.15)),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const style: CSSProperties = {
    position: "absolute",
    top: "17%",
    [position]: "9%",
    width: 104,
    height: 104,
    opacity,
    transform: `scale(${scale})`,
    filter: "drop-shadow(0 3px 7px rgba(0,0,0,0.18))",
  };

  return (
    <svg viewBox="0 0 100 100" style={style} aria-hidden="true">
      {kind === "drop" ? (
        <>
          <path
            d="M50 7C39 25 23 43 23 60a27 27 0 0 0 54 0C77 43 61 25 50 7Z"
            fill="#c8edf0"
            stroke="#5d9da8"
            strokeWidth="3"
          />
          <path
            d="M38 61c0-8 5-15 11-23"
            fill="none"
            stroke="#fff"
            strokeWidth="4"
            strokeLinecap="round"
            opacity="0.9"
          />
        </>
      ) : (
        <>
          <path
            d="M50 42C37 28 23 29 16 38c-7 10 4 24 13 33 8 8 15 11 21 15 6-4 13-7 21-15 9-9 20-23 13-33-7-9-21-10-34 4Z"
            fill="#df6377"
          />
          <path
            d="M50 42c-8-7-17-8-24-5M50 42c8-7 17-8 24-5"
            fill="none"
            stroke="#f7b0b9"
            strokeWidth="3"
            strokeLinecap="round"
            opacity="0.8"
          />
        </>
      )}
    </svg>
  );
};

const PhaseOneSample: FC<{
  sourceFile: string;
  sourceDurationInFrames: number;
  audioFile: string | null;
  wordTimings: TimedWord[];
}> = ({ sourceFile, sourceDurationInFrames, audioFile, wordTimings }) => {
  const keywordWindows = useMemo(
    () =>
      SEGMENTS.map((segment) =>
        keywordWindowsForSegment(wordTimings, segment, FPS),
      ),
    [wordTimings],
  );

  return (
    <AbsoluteFill style={{ backgroundColor: "#09090b" }}>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={SEGMENTS[0].frames}>
          <Clip
            sourceFile={sourceFile}
            sourceDurationInFrames={sourceDurationInFrames}
            sourceStart={SEGMENTS[0].sourceStart}
            frames={SEGMENTS[0].frames}
            keywordWindows={keywordWindows[0]}
          />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition
          presentation={fade()}
          timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
        />
        <TransitionSeries.Sequence durationInFrames={SEGMENTS[1].frames}>
          <Clip
            sourceFile={sourceFile}
            sourceDurationInFrames={sourceDurationInFrames}
            sourceStart={SEGMENTS[1].sourceStart}
            frames={SEGMENTS[1].frames}
            keywordWindows={keywordWindows[1]}
          />
        </TransitionSeries.Sequence>
        <TransitionSeries.Transition
          presentation={fade()}
          timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
        />
        <TransitionSeries.Sequence durationInFrames={SEGMENTS[2].frames}>
          <Clip
            sourceFile={sourceFile}
            sourceDurationInFrames={sourceDurationInFrames}
            sourceStart={SEGMENTS[2].sourceStart}
            frames={SEGMENTS[2].frames}
            keywordWindows={keywordWindows[2]}
          />
        </TransitionSeries.Sequence>
      </TransitionSeries>

      {SEGMENTS.map((segment, index) => (
        <AudioSegment
          audioFile={audioFile}
          key={segment.sourceStart}
          sourceStart={segment.sourceStart}
          frames={segment.frames}
          outputStart={segment.outputStart}
          fadeInFrames={index === 0 ? 0 : TRANSITION_FRAMES}
          fadeOutFrames={index === SEGMENTS.length - 1 ? 0 : TRANSITION_FRAMES}
        />
      ))}

      <Sequence from={FPS} durationInFrames={STICKER_DURATION_FRAMES} layout="none">
        <AnimatedSticker kind="drop" position="left" />
      </Sequence>
      <Sequence from={FPS * 3} durationInFrames={STICKER_DURATION_FRAMES} layout="none">
        <AnimatedSticker kind="kiss" position="right" />
      </Sequence>
      <WordSubtitles words={wordTimings} fps={FPS} />
    </AbsoluteFill>
  );
};

export const RemotionRoot: FC = () => (
  <Composition
    id="MATRIX-MOTION-Remotion-Phase1"
    component={PhaseOneSample}
    width={1080}
    height={1920}
    fps={FPS}
    durationInFrames={DURATION_IN_FRAMES}
    defaultProps={{
      sourceFile: "source.mov",
       sourceDurationInFrames: 360,
      audioFile: null,
      wordTimings: [],
    }}
  />
);
