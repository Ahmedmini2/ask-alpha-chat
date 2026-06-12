import React, { useMemo } from "react";
import {
  AbsoluteFill,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  Caption,
  createTikTokStyleCaptions,
  TikTokPage,
} from "@remotion/captions";
import { loadFont } from "@remotion/google-fonts/Montserrat";
import { CaptionToken } from "./Root";

// Heavy weight is the Hormozi look. Montserrat 900, uppercase.
const { fontFamily } = loadFont("normal", { weights: ["900"], subsets: ["latin"] });

const FILLED = "#FFE600"; // spoken words → bright yellow
const UNFILLED = "#FFFFFF"; // upcoming words → white
const STROKE = "#000000";
// Group word tokens into short karaoke phrases (~one breath).
const COMBINE_MS = 1200;

export const Captions: React.FC<{ captions: CaptionToken[] }> = ({
  captions,
}) => {
  const frame = useCurrentFrame();
  const { fps, width } = useVideoConfig();
  const timeMs = (frame / fps) * 1000;

  const pages = useMemo<TikTokPage[]>(() => {
    if (!captions || captions.length === 0) return [];
    const normalized: Caption[] = captions
      .filter((c) => (c.text ?? "").trim().length > 0)
      .map((c) => ({
        // @remotion/captions joins tokens by their text; a leading space keeps words apart.
        text: c.text.startsWith(" ") ? c.text : ` ${c.text}`,
        startMs: c.startMs,
        endMs: c.endMs,
        timestampMs: (c.startMs + c.endMs) / 2,
        confidence: null,
      }));
    return createTikTokStyleCaptions({
      captions: normalized,
      combineTokensWithinMilliseconds: COMBINE_MS,
    }).pages;
  }, [captions]);

  const activePage = pages.find(
    (p) => timeMs >= p.startMs && timeMs < p.startMs + p.durationMs
  );
  if (!activePage) return null;

  return (
    <Page page={activePage} timeMs={timeMs} fps={fps} width={width} />
  );
};

const Page: React.FC<{
  page: TikTokPage;
  timeMs: number;
  fps: number;
  width: number;
}> = ({ page, timeMs, fps, width }) => {
  // Pop-in: the page scales 0.86 → 1.0 over the first ~8 frames it's on screen.
  const enterFrame = ((timeMs - page.startMs) / 1000) * fps;
  const pop = spring({
    frame: enterFrame,
    fps,
    config: { damping: 200 },
    durationInFrames: 8,
  });
  const scale = 0.86 + pop * 0.14;

  const fontSize = Math.round(width * 0.072);
  const strokeW = Math.max(2, Math.round(fontSize * 0.06));

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: "22%",
      }}
    >
      <div
        style={{
          transform: `scale(${scale})`,
          maxWidth: "86%",
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: "0.1em 0.28em",
          textAlign: "center",
        }}
      >
        {page.tokens.map((t, i) => {
          const spoken = timeMs >= t.fromMs; // karaoke fill, left-to-right
          return (
            <span
              key={`${i}-${t.fromMs}`}
              style={{
                fontFamily,
                fontWeight: 900,
                fontSize,
                lineHeight: 1.05,
                textTransform: "uppercase",
                color: spoken ? FILLED : UNFILLED,
                WebkitTextStroke: `${strokeW}px ${STROKE}`,
                paintOrder: "stroke fill",
                textShadow: "0 4px 16px rgba(0,0,0,0.65)",
              }}
            >
              {t.text.trim()}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
