import {
  AbsoluteFill,
  Audio,
  Composition,
  Easing,
  Img,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";
import type { CSSProperties, FC } from "react";
import { WordSubtitles } from "./WordSubtitles";
import type { CaptionStyle } from "./WordSubtitles";
import type { TimedWord } from "./caption-timing.mjs";
import type { TimedVectorEffect } from "./full-edit-timeline.mjs";
import {
  FULL_DURATION_IN_FRAMES,
  FULL_FPS,
  FULL_HEIGHT,
  FULL_WIDTH,
} from "./full-edit-timeline.mjs";

type FullEditProps = {
  durationInFrames: number;
  effects: TimedVectorEffect[];
  wordTimings: TimedWord[];
  showSubtitles: boolean;
  soundEffects: SoundEffectCue[];
  captionStyle: CaptionStyle;
};

export type SoundEffectCue = {
  kind: "swoosh" | "pop" | "ding" | "impact";
  startFrame: number;
  durationFrames: number;
};

const SOUND_EFFECT_FILES: Record<SoundEffectCue["kind"], string> = {
  swoosh: "swoosh.wav",
  pop: "pop.wav",
  ding: "ding.wav",
  impact: "impact.wav",
};

const SoundEffect: FC<{ cue: SoundEffectCue }> = ({ cue }) => (
  <Sequence
    from={cue.startFrame}
    durationInFrames={cue.durationFrames}
    layout="none"
  >
    <Audio
      src={staticFile(`sfx/${SOUND_EFFECT_FILES[cue.kind]}`)}
      volume={0.45}
    />
  </Sequence>
);

const VectorOverlay: FC<{ effect: TimedVectorEffect }> = ({ effect }) => {
  const frame = useCurrentFrame();
  const fadeIn = Math.min(8, Math.floor(effect.durationFrames / 4));
  const fadeOut = Math.min(7, Math.floor(effect.durationFrames / 4));
  const opacity = Math.min(
    interpolate(frame, [0, fadeIn], [0, 1], {
      easing: Easing.out(Easing.quad),
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
    interpolate(
      frame,
      [effect.durationFrames - fadeOut, effect.durationFrames],
      [1, 0],
      {
        easing: Easing.in(Easing.quad),
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      },
    ),
  );
  const scale = interpolate(frame, [0, 12], [0.97, 1], {
    easing: Easing.out(Easing.quad),
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
  effects,
  wordTimings,
  showSubtitles,
  soundEffects,
  captionStyle,
}) => (
  <AbsoluteFill style={{ backgroundColor: "transparent" }}>
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
    <WordSubtitles
      words={wordTimings}
      fps={FULL_FPS}
      showSubtitles={showSubtitles}
      captionStyle={captionStyle}
    />
    {soundEffects.map((cue, index) => (
      <SoundEffect key={`${cue.kind}-${cue.startFrame}-${index}`} cue={cue} />
    ))}
  </AbsoluteFill>
);

export const FullRemotionRoot: FC = () => (
  <Composition
    id="MATRIX-MOTION-Master-Overlay"
    component={FullEdit}
    width={FULL_WIDTH}
    height={FULL_HEIGHT}
    fps={FULL_FPS}
    durationInFrames={FULL_DURATION_IN_FRAMES}
    defaultProps={{
      durationInFrames: FULL_DURATION_IN_FRAMES,
      effects: [],
      wordTimings: [],
      showSubtitles: true,
      soundEffects: [],
      captionStyle: {
        fontFamily: "Montserrat",
        highlightColor: "#FF2A85",
        textColor: "#FFFFFF",
      },
    }}
  />
);
