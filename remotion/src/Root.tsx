import { Composition, getInputProps } from "remotion";
import { getVideoMetadata } from "@remotion/media-utils";
import { z } from "zod";
import { CaptionedVideo } from "./CaptionedVideo";

export const FPS = 30;

// One caption token = one spoken word with its start/end in milliseconds.
export const captionTokenSchema = z.object({
  text: z.string(),
  startMs: z.number(),
  endMs: z.number(),
});

export const captionedVideoSchema = z.object({
  // Source video: an http(s) URL or a file:// path (Python passes a local file://).
  src: z.string(),
  captions: z.array(captionTokenSchema),
});

export type CaptionToken = z.infer<typeof captionTokenSchema>;

const DEFAULT_DURATION_FRAMES = 30 * 60; // fallback: 60s @ 30fps

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="CaptionedVideo"
      component={CaptionedVideo}
      schema={captionedVideoSchema}
      // Sensible defaults so the composition is openable in the Studio without props.
      durationInFrames={DEFAULT_DURATION_FRAMES}
      fps={FPS}
      width={1080}
      height={1920}
      defaultProps={{
        src: getInputProps().src ?? "",
        captions: (getInputProps().captions as CaptionToken[]) ?? [],
      }}
      // Match the canvas to the actual source video (9:16, real duration) so the
      // composited captions line up frame-for-frame with the avatar's speech.
      calculateMetadata={async ({ props }) => {
        if (!props.src) {
          return {
            durationInFrames: DEFAULT_DURATION_FRAMES,
            fps: FPS,
            width: 1080,
            height: 1920,
          };
        }
        const meta = await getVideoMetadata(props.src);
        return {
          durationInFrames: Math.max(1, Math.ceil(meta.durationInSeconds * FPS)),
          fps: FPS,
          width: meta.width,
          height: meta.height,
        };
      }}
    />
  );
};
