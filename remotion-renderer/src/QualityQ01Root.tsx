import React, { FC } from "react";
import { Composition, staticFile, Video } from "remotion";

const QualityQ01Video: FC = () => (
  <Video src={staticFile("q01-source-3s.mp4")} />
);

export const QualityQ01Root: FC = () => (
  <Composition
    id="MATRIX-MOTION-Quality-Q01"
    component={QualityQ01Video}
    durationInFrames={150}
    fps={50}
    width={1080}
    height={1920}
  />
);
