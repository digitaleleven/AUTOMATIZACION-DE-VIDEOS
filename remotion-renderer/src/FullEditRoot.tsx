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
import { CameraMotionBlur } from "@remotion/motion-blur";
import type { CSSProperties, FC } from "react";
import { WordSubtitles } from "./WordSubtitles";
import type { CaptionStyle } from "./WordSubtitles";
import type { TimedWord, CaptionGroup } from "./caption-timing.mjs";
import type { TimedVectorEffect } from "./full-edit-timeline.mjs";
import {
  FULL_DURATION_IN_FRAMES,
  FULL_FPS,
  FULL_HEIGHT,
  FULL_WIDTH,
} from "./full-edit-timeline.mjs";

export type SoundEffectCue = {
  kind: "swoosh" | "pop" | "ding" | "impact";
  startFrame: number;
  durationFrames: number;
};

export type OverlayPosition =
  | "topLeft"
  | "topRight"
  | "left"
  | "right"
  | "bottomLeft"
  | "bottomRight";

export type TimedImageOverlay = {
  id: string;
  asset: string;
  label: string;
  position: OverlayPosition;
  startFrame: number;
  durationFrames: number;
};

export type TimedCardOverlay = {
  id: string;
  text: string;
  position: OverlayPosition;
  startFrame: number;
  durationFrames: number;
};

export type TimedTitleOverlay = {
  text: string;
  durationFrames: number;
};

type FullEditProps = {
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  effects: TimedVectorEffect[];
  wordTimings: TimedWord[];
  showSubtitles: boolean;
  soundEffects: SoundEffectCue[];
  captionStyle: CaptionStyle;
  titleOverlay: TimedTitleOverlay | null;
  brandLogoAsset: string | null;
  promoText: string | null;
  imageOverlays: TimedImageOverlay[];
  cardOverlays: TimedCardOverlay[];
  captionGroups: CaptionGroup[] | undefined;
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

const OVERLAY_POSITION_STYLE: Record<OverlayPosition, CSSProperties> = {
  topLeft: { top: "27%", left: "5%" },
  topRight: { top: "27%", right: "5%" },
  left: { top: "44%", left: "5%" },
  right: { top: "44%", right: "5%" },
  bottomLeft: { bottom: "22%", left: "5%" },
  bottomRight: { bottom: "22%", right: "5%" },
};

const overlayOpacity = (frame: number, durationFrames: number) =>
  interpolate(frame, [0, Math.min(10, durationFrames)], [0, 1], {
    easing: Easing.out(Easing.quad),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

const overlayScale = (frame: number) =>
  interpolate(frame, [0, 12], [0.88, 1], {
    easing: Easing.out(Easing.back(1.4)),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

const TitleOverlay: FC<{ title: TimedTitleOverlay }> = ({ title }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
      <div
        style={{
          position: "absolute",
          top: "8%",
          left: "8%",
          right: "8%",
          color: "#FFFDF8",
          fontFamily: 'Georgia, "Times New Roman", serif',
          fontSize: 70,
          fontWeight: 700,
          letterSpacing: "-0.02em",
          textAlign: "center",
          lineHeight: 1.06,
          opacity: overlayOpacity(frame, title.durationFrames),
          transform: `scale(${overlayScale(frame)})`,
          textShadow: "0 3px 12px rgba(20,16,12,0.48)",
        }}
      >
        {title.text}
      </div>
    </AbsoluteFill>
  );
};

const ClientBrandLogoOverlay: FC<{ asset: string }> = ({ asset }) => {
  const frame = useCurrentFrame();
  return (
    <CameraMotionBlur shutterAngle={90} samples={3}>
      <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
        <div
          style={{
            position: "absolute",
            top: "4%",
            left: "50%",
            width: "58%",
            height: 112,
            padding: "10px 18px",
            boxSizing: "border-box",
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            transform: `translateX(-50%) scale(${overlayScale(frame)})`,
            transformOrigin: "center top",
            opacity: overlayOpacity(frame, 18),
            borderRadius: 16,
            backgroundColor: "rgba(18,18,18,0.34)",
            boxShadow: "0 4px 18px rgba(0,0,0,0.2)",
          }}
        >
          <Img
            src={staticFile(`overlays/${asset}`)}
            style={{
              display: "block",
              width: "100%",
              height: "100%",
              objectFit: "contain",
              filter: "drop-shadow(0 2px 8px rgba(0,0,0,0.3))",
            }}
          />
        </div>
      </AbsoluteFill>
    </CameraMotionBlur>
  );
};

const PromoOverlay: FC<{ text: string }> = ({ text }) => (
  <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
    <div
      style={{
        position: "absolute",
        top: "18%",
        left: "5%",
        maxWidth: "58%",
        padding: "12px 18px",
        borderRadius: 14,
        backgroundColor: "rgba(24,27,27,0.74)",
        color: "#FFFDF8",
        fontFamily: '"Montserrat", Arial, sans-serif',
        fontSize: 30,
        fontWeight: 750,
        lineHeight: 1.15,
        boxShadow: "0 5px 18px rgba(0,0,0,0.24)",
        overflowWrap: "anywhere",
      }}
    >
      {text}
    </div>
  </AbsoluteFill>
);

const PictureInPictureOverlay: FC<{ cue: TimedImageOverlay }> = ({ cue }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
      <div
        style={{
          position: "absolute",
          ...OVERLAY_POSITION_STYLE[cue.position],
          width: 300,
          maxWidth: "31%",
          opacity: overlayOpacity(frame, cue.durationFrames),
          transform: `scale(${overlayScale(frame)})`,
          transformOrigin: "center",
          borderRadius: 16,
          overflow: "hidden",
          backgroundColor: "rgba(255,253,248,0.96)",
          boxShadow: "0 8px 26px rgba(0,0,0,0.30)",
        }}
      >
        <Img
          src={staticFile(`overlays/${cue.asset}`)}
          style={{ display: "block", width: "100%", maxHeight: 360, objectFit: "contain" }}
        />
        {cue.label && (
          <div
            style={{
              padding: "9px 12px",
              color: "#272522",
              fontFamily: '"Montserrat", Arial, sans-serif',
              fontSize: 20,
              fontWeight: 700,
              textAlign: "center",
            }}
          >
            {cue.label}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};

const ProductCardOverlay: FC<{ cue: TimedCardOverlay }> = ({ cue }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }} aria-hidden="true">
      <div
        style={{
          position: "absolute",
          ...OVERLAY_POSITION_STYLE[cue.position],
          width: 330,
          maxWidth: "34%",
          padding: "20px 22px",
          borderRadius: 16,
          backgroundColor: "rgba(255,253,248,0.94)",
          color: "#2D2825",
          fontFamily: 'Georgia, "Times New Roman", serif',
          fontSize: 30,
          fontWeight: 700,
          lineHeight: 1.18,
          textAlign: "center",
          opacity: overlayOpacity(frame, cue.durationFrames),
          transform: `scale(${overlayScale(frame)})`,
          transformOrigin: "center",
          boxShadow: "0 8px 26px rgba(0,0,0,0.28)",
          overflowWrap: "anywhere",
        }}
      >
        {cue.text}
      </div>
    </AbsoluteFill>
  );
};

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
  width,
  height,
  fps,
  durationInFrames,
  effects,
  wordTimings,
  showSubtitles,
  soundEffects,
  captionStyle,
  titleOverlay,
  brandLogoAsset,
  promoText,
  imageOverlays,
  cardOverlays,
  captionGroups,
}) => (
  <AbsoluteFill style={{ backgroundColor: "transparent", overflow: "hidden" }}>
    <div
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: FULL_WIDTH,
        height: FULL_HEIGHT,
        transform: `scale(${width / FULL_WIDTH})`,
        transformOrigin: "top left",
      }}
    >
    {titleOverlay && (
      <Sequence from={0} durationInFrames={titleOverlay.durationFrames} layout="none">
        <TitleOverlay title={titleOverlay} />
      </Sequence>
    )}
    {brandLogoAsset && (
      <Sequence from={0} durationInFrames={durationInFrames} layout="none">
        <ClientBrandLogoOverlay asset={brandLogoAsset} />
      </Sequence>
    )}
    {promoText && <PromoOverlay text={promoText} />}
    {imageOverlays.map((cue) => (
      <Sequence key={cue.id} from={cue.startFrame} durationInFrames={cue.durationFrames} layout="none">
        <PictureInPictureOverlay cue={cue} />
      </Sequence>
    ))}
    {cardOverlays.map((cue) => (
      <Sequence key={cue.id} from={cue.startFrame} durationInFrames={cue.durationFrames} layout="none">
        <ProductCardOverlay cue={cue} />
      </Sequence>
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
    <WordSubtitles
      words={wordTimings}
      fps={fps}
      showSubtitles={showSubtitles}
      captionStyle={captionStyle}
      captionGroups={captionGroups}
    />
    {soundEffects.map((cue, index) => (
      <SoundEffect key={`${cue.kind}-${cue.startFrame}-${index}`} cue={cue} />
    ))}
    </div>
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
    calculateMetadata={({ props }) => ({
      width: props.width,
      height: props.height,
      fps: props.fps,
      durationInFrames: props.durationInFrames,
    })}
    defaultProps={{
      width: FULL_WIDTH,
      height: FULL_HEIGHT,
      fps: FULL_FPS,
      durationInFrames: FULL_DURATION_IN_FRAMES,
      effects: [],
      wordTimings: [],
      showSubtitles: true,
      soundEffects: [],
      titleOverlay: null,
      brandLogoAsset: null,
      promoText: null,
      imageOverlays: [],
      cardOverlays: [],
      captionGroups: undefined,
      captionStyle: {
        fontFamily: "Montserrat",
        highlightColor: "#FFC0CB",
        textColor: "#202124",
        fontSize: 54,
        position: "bottom",
        maxWordsPerCaption: 2,
      },
    }}
  />
);
