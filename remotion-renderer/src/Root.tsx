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

const FPS = 50;
const SOURCE_FRAMES = 941;
const TRANSITION_FRAMES = 20;
const SEGMENTS = [
  { sourceStart: 0, frames: 100, outputStart: 0 },
  { sourceStart: 250, frames: 125, outputStart: 80 },
  { sourceStart: 500, frames: 100, outputStart: 185 },
] as const;
const DURATION_IN_FRAMES = 285;

type ClipProps = {
  sourceStart: number;
  frames: number;
  zoom: boolean;
};

const Clip: FC<ClipProps> = ({ sourceStart, frames, zoom }) => {
  const frame = useCurrentFrame();
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
        src={staticFile("0415-source.mov")}
        trimBefore={sourceStart}
        trimAfter={SOURCE_FRAMES - sourceStart - frames}
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
  sourceStart: number;
  frames: number;
  outputStart: number;
  fadeInFrames: number;
  fadeOutFrames: number;
}> = ({ sourceStart, frames, outputStart, fadeInFrames, fadeOutFrames }) => (
  <Sequence from={outputStart} durationInFrames={frames} layout="none">
    <Audio
      src={staticFile("0415-audio.wav")}
      trimBefore={sourceStart}
      durationInFrames={frames}
      volume={(frame) =>
        audioVolume(frame, frames, fadeInFrames, fadeOutFrames)
      }
    />
  </Sequence>
);

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
    filter: "drop-shadow(0 0 28px rgba(255, 205, 92, 0.9))",
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

const PhaseOneSample: FC = () => {
  const source = staticFile("0415-source.mov");

  return (
    <AbsoluteFill style={{ backgroundColor: "#09090b" }}>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={SEGMENTS[0].frames}>
          <Clip
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
            sourceStart={SEGMENTS[2].sourceStart}
            frames={SEGMENTS[2].frames}
            zoom={false}
          />
        </TransitionSeries.Sequence>
      </TransitionSeries>

      {SEGMENTS.map((segment, index) => (
        <AudioSegment
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
  />
);
