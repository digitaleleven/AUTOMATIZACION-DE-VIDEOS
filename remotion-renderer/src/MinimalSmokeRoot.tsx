import React, { FC } from "react";
import { Composition, staticFile, Video } from "remotion";

const MinimalVideo: FC = () => (
  <Video src={staticFile("optimized-source-3s.mp4")} />
);

export const MinimalSmokeRoot: FC = () => (
  <Composition
    id="MATRIX-MOTION-Minimal-Smoke"
    component={MinimalVideo}
    durationInFrames={75}
    fps={25}
    width={1080}
    height={1920}
  />
);
