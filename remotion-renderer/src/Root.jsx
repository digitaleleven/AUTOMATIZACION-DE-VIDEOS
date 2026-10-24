import { AbsoluteFill, Composition, OffthreadVideo, staticFile } from "remotion";

const Sample = ({ sourceFile, trimAfter }) => (
  <AbsoluteFill style={{ backgroundColor: "#000" }}>
    <OffthreadVideo
      src={staticFile(sourceFile)}
      trimBefore={0}
      trimAfter={trimAfter}
      style={{ width: "100%", height: "100%", objectFit: "contain" }}
    />
  </AbsoluteFill>
);

export const RemotionRoot = () => (
  <Composition
    id="RemotionLocalSample"
    component={Sample}
    width={1080}
    height={1920}
    fps={50}
    durationInFrames={250}
    defaultProps={{ sourceFile: "source.mov", trimAfter: 0 }}
  />
);
