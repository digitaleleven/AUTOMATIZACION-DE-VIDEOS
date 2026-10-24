import React, { FC } from "react";
import { Audio, Composition, staticFile, Video } from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";

const FPS = 50;
const WIDTH = 1080;
const HEIGHT = 1920;
const TRANSITION_FRAMES = 20;

const segments = [
  { sourceStart: 0, duration: 300, fadeIn: false, fadeOut: true },
  { sourceStart: 320, duration: 290, fadeIn: true, fadeOut: true },
  { sourceStart: 630, duration: 311, fadeIn: true, fadeOut: false },
] as const;

const compositionDuration =
  segments.reduce((sum, segment) => sum + segment.duration, 0) -
  TRANSITION_FRAMES * (segments.length - 1);

const audioEnvelope =
  (duration: number, fadeIn: boolean, fadeOut: boolean) => (frame: number) => {
    if (fadeIn && frame < TRANSITION_FRAMES) {
      return frame / (TRANSITION_FRAMES - 1);
    }
    if (fadeOut && frame >= duration - TRANSITION_FRAMES) {
      return (duration - 1 - frame) / (TRANSITION_FRAMES - 1);
    }
    return 1;
  };

const EditedQ02Video: FC = () => (
  <TransitionSeries>
    <TransitionSeries.Sequence durationInFrames={segments[0].duration}>
      <Video
        src={staticFile("q02-source-1080.mp4")}
        trimBefore={segments[0].sourceStart}
        durationInFrames={segments[0].duration}
        muted
      />
      <Audio
        src={staticFile("q02-master-audio.m4a")}
        trimBefore={segments[0].sourceStart}
        durationInFrames={segments[0].duration}
        volume={audioEnvelope(
          segments[0].duration,
          segments[0].fadeIn,
          segments[0].fadeOut,
        )}
      />
    </TransitionSeries.Sequence>
    <TransitionSeries.Transition
      presentation={fade()}
      timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
    />
    <TransitionSeries.Sequence durationInFrames={segments[1].duration}>
      <Video
        src={staticFile("q02-source-1080.mp4")}
        trimBefore={segments[1].sourceStart}
        durationInFrames={segments[1].duration}
        muted
      />
      <Audio
        src={staticFile("q02-master-audio.m4a")}
        trimBefore={segments[1].sourceStart}
        durationInFrames={segments[1].duration}
        volume={audioEnvelope(
          segments[1].duration,
          segments[1].fadeIn,
          segments[1].fadeOut,
        )}
      />
    </TransitionSeries.Sequence>
    <TransitionSeries.Transition
      presentation={fade()}
      timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
    />
    <TransitionSeries.Sequence durationInFrames={segments[2].duration}>
      <Video
        src={staticFile("q02-source-1080.mp4")}
        trimBefore={segments[2].sourceStart}
        durationInFrames={segments[2].duration}
        muted
      />
      <Audio
        src={staticFile("q02-master-audio.m4a")}
        trimBefore={segments[2].sourceStart}
        durationInFrames={segments[2].duration}
        volume={audioEnvelope(
          segments[2].duration,
          segments[2].fadeIn,
          segments[2].fadeOut,
        )}
      />
    </TransitionSeries.Sequence>
  </TransitionSeries>
);

export const QualityQ02Root: FC = () => (
  <Composition
    id="MATRIX-MOTION-Quality-Q02"
    component={EditedQ02Video}
    durationInFrames={compositionDuration}
    fps={FPS}
    width={WIDTH}
    height={HEIGHT}
  />
);
