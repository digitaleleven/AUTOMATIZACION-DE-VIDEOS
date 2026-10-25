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
import type { CSSProperties, FC } from "react";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { WordSubtitles } from "./WordSubtitles";
import type { TimedWord } from "./caption-timing.mjs";
import {
  DURATION_IN_FRAMES,
  FPS,
  SEGMENTS,
  TRANSITION_FRAMES,
  getSourceClipWindow,
} from "./edit-timeline.mjs";

type ClipProps = {
  sourceFile: string;
  sourceDurationInFrames: number;
  sourceStart: number;
  frames: number;
  zoom: boolean;
};

const Clip: FC<ClipProps> = ({
  sourceFile,
  sourceDurationInFrames,
  sourceStart,
  frames,
  zoom,
}) => {
  const frame = useCurrentFrame();
  const sourceClip = getSourceClipWindow(
    sourceStart,
    frames,
    sourceDurationInFrames,
  );
  const scale = zoom
    ? interpolate(frame, [0, frames - 1], [1, 1.12], {
        easing: Easing.inOut(Easing.quad),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      })
    : 1;

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
        trimBefore={sourceStart / FPS}
        durationInFrames={frames}
        volume={(frame) =>
          audioVolume(frame, frames, fadeInFrames, fadeOutFrames)
        }
      />
    </Sequence>
  ) : null;

const AnimatedSparkle: FC = () => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 8, 48, 60], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const scale = interpolate(frame, [0, 30, 60], [0.45, 1.08, 0.8], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const rotation = interpolate(frame, [0, 60], [-18, 36]);
  const style: CSSProperties = {
    position: "absolute",
    top: "16%",
    right: "12%",
    width: 190,
    height: 190,
    opacity,
    transform: `scale(${scale}) rotate(${rotation}deg)`,
  };

  return (
    <svg viewBox="0 0 200 200" style={style} aria-hidden="true">
      <path
        d="M100 8 119 75 185 100 119 120 100 192 80 120 15 100 80 75Z"
        fill="#fff2bb"
        stroke="#f7bd50"
        strokeWidth="6"
        strokeLinejoin="round"
      />
      <circle cx="153" cy="48" r="13" fill="#fff8df" />
      <circle cx="43" cy="151" r="9" fill="#f7bd50" />
    </svg>
  );
};

const PhaseOneSample: FC<{
  sourceFile: string;
  sourceDurationInFrames: number;
  audioFile: string | null;
  wordTimings: TimedWord[];
}> = ({ sourceFile, sourceDurationInFrames, audioFile, wordTimings }) => {
  return (
    <AbsoluteFill style={{ backgroundColor: "#09090b" }}>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={SEGMENTS[0].frames}>
          <Clip
            sourceFile={sourceFile}
            sourceDurationInFrames={sourceDurationInFrames}
            sourceStart={SEGMENTS[0].sourceStart}
            frames={SEGMENTS[0].frames}
            zoom={false}
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
            zoom
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
            zoom={false}
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

      <Sequence from={50} durationInFrames={60} layout="none">
        <AnimatedSparkle />
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
      sourceDurationInFrames: 941,
      audioFile: null,
      wordTimings: [],
    }}
  />
);
