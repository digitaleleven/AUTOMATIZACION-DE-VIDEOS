import {
  AbsoluteFill,
  Audio,
  Composition,
  Easing,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";
import { useMemo } from "react";
import type { CSSProperties, FC } from "react";
import { WordSubtitles } from "./WordSubtitles";
import { keywordWindowsForSegment } from "./caption-timing.mjs";
import type { TimedWord } from "./caption-timing.mjs";
import type { KeepSegment, TimedVectorEffect } from "./full-edit-timeline.mjs";
import { getSourceClipWindow } from "./edit-timeline.mjs";
import {
  FULL_DURATION_IN_FRAMES,
  FULL_FPS,
} from "./full-edit-timeline.mjs";

type FullEditProps = {
  sourceFile: string;
  audioFile: string;
  sourceDurationInFrames: number;
  durationInFrames: number;
  clips: KeepSegment[];
  effects: TimedVectorEffect[];
  wordTimings: TimedWord[];
};

const VideoClip: FC<{
  sourceFile: string;
  sourceDurationInFrames: number;
  clip: KeepSegment;
  words: TimedWord[];
}> = ({ sourceFile, sourceDurationInFrames, clip, words }) => {
  const frame = useCurrentFrame();
  const sourceWindow = getSourceClipWindow(
    clip.sourceStart,
    clip.frames,
    sourceDurationInFrames,
  );
  const keywordWindows = useMemo(
    () => keywordWindowsForSegment(words, clip, FULL_FPS),
    [words, clip],
  );
  const scale = keywordWindows.reduce((largest, window) => {
    const span = Math.max(1, window.endFrame - window.startFrame);
    const ramp = Math.max(1, Math.min(5, Math.floor(span / 2)));
    const current = interpolate(
      frame,
      [window.startFrame - ramp, window.startFrame, window.endFrame, window.endFrame + ramp],
      [1, 1.05, 1.12, 1],
      {
        easing: Easing.inOut(Easing.quad),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      },
    );
    return Math.max(largest, current);
  }, 1);

  return (
    <AbsoluteFill style={{ overflow: "hidden", backgroundColor: "#08090b" }}>
      <OffthreadVideo
        src={staticFile(sourceFile)}
        trimBefore={sourceWindow.trimBefore}
        durationInFrames={sourceWindow.durationInFrames}
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

const AudioClip: FC<{
  audioFile: string;
  clip: KeepSegment;
}> = ({ audioFile, clip }) => (
  <Sequence from={clip.outputStart} durationInFrames={clip.frames} layout="none">
    <Audio
      src={staticFile(audioFile)}
      trimBefore={clip.sourceStart}
      durationInFrames={clip.frames}
      volume={1}
    />
  </Sequence>
);

const VectorOverlay: FC<{ effect: TimedVectorEffect }> = ({ effect }) => {
  const frame = useCurrentFrame();
  const fadeIn = Math.min(8, Math.floor(effect.durationFrames / 4));
  const fadeOut = Math.min(7, Math.floor(effect.durationFrames / 4));
  const opacity = Math.min(
    interpolate(frame, [0, fadeIn], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
    interpolate(
      frame,
      [effect.durationFrames - fadeOut, effect.durationFrames],
      [1, 0],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    ),
  );
  const scale = interpolate(frame, [0, 9], [0.78, 1], {
    easing: Easing.out(Easing.back(1.1)),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const isPair = effect.assets.length > 1;
  const containerStyle: CSSProperties = {
    position: "absolute",
    top: "15%",
    right: "8%",
    display: "flex",
    alignItems: "center",
    gap: 8,
    opacity,
    transform: `scale(${scale})`,
    transformOrigin: "center",
    pointerEvents: "none",
  };

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
      <div style={containerStyle}>
        {effect.assets.map((asset) => (
          <Img
            key={asset}
            src={staticFile(`effects/${asset}`)}
            style={{
              width: isPair ? 92 : 142,
              height: isPair ? 92 : 142,
              objectFit: "contain",
              filter: "drop-shadow(0 4px 10px rgba(0,0,0,0.22))",
            }}
          />
        ))}
      </div>
    </AbsoluteFill>
  );
};

const FullEdit: FC<FullEditProps> = ({
  sourceFile,
  audioFile,
  sourceDurationInFrames,
  clips,
  effects,
  wordTimings,
}) => (
  <AbsoluteFill style={{ backgroundColor: "#08090b" }}>
    {clips.map((clip) => (
      <Sequence
        key={`video-${clip.sourceStart}`}
        from={clip.outputStart}
        durationInFrames={clip.frames}
        layout="none"
      >
        <VideoClip
          sourceFile={sourceFile}
          sourceDurationInFrames={sourceDurationInFrames}
          clip={clip}
          words={wordTimings}
        />
      </Sequence>
    ))}
    {clips.map((clip) => (
      <AudioClip key={`audio-${clip.sourceStart}`} audioFile={audioFile} clip={clip} />
    ))}
    {effects.map((effect) => (
      <Sequence
        key={effect.id}
        from={effect.startFrame}
        durationInFrames={effect.durationFrames}
        layout="none"
      >
        <VectorOverlay effect={effect} />
      </Sequence>
    ))}
    <WordSubtitles words={wordTimings} fps={FULL_FPS} />
  </AbsoluteFill>
);

export const FullRemotionRoot: FC = () => (
  <Composition
    id="MATRIX-MOTION-Remotion-Full"
    component={FullEdit}
    width={1080}
    height={1920}
    fps={FULL_FPS}
    durationInFrames={FULL_DURATION_IN_FRAMES}
    defaultProps={{
      sourceFile: "source-proxy.mp4",
      audioFile: "source-audio.wav",
      sourceDurationInFrames: 1800,
      durationInFrames: FULL_DURATION_IN_FRAMES,
      clips: [],
      effects: [],
      wordTimings: [],
    }}
  />
);
