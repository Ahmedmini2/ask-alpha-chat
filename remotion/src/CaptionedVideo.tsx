import { AbsoluteFill, OffthreadVideo } from "remotion";
import { z } from "zod";
import { captionedVideoSchema } from "./Root";
import { Captions } from "./Captions";

export const CaptionedVideo: React.FC<z.infer<typeof captionedVideoSchema>> = ({
  src,
  captions,
}) => {
  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {src ? (
        <OffthreadVideo src={src} />
      ) : null}
      <Captions captions={captions} />
    </AbsoluteFill>
  );
};
