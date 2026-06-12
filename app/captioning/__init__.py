"""Post-process agent promo videos: transcribe + burn Hormozi-style captions.

HeyGen's video-agent export ships without burned-in subtitles, so after a video
finishes we download it, transcribe the audio to word-level timestamps with
faster-whisper, render karaoke-fill captions over it with Remotion, and upload
the result to S3. The pipeline is best-effort: any failure falls back to the raw
HeyGen video so delivery is never blocked (see app/workers/heygen_poller.py).
"""
