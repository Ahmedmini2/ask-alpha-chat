import { Config } from "@remotion/cli/config";

// H.264 MP4 out — universally playable (Telegram inline, browsers, mobile).
Config.setVideoImageFormat("jpeg");
Config.setCodec("h264");
// Headless rendering on small/headless boxes (Railway, WSL): no GPU sandbox.
Config.setChromiumOpenGlRenderer("angle");
Config.setChromiumDisableWebSecurity(true);
