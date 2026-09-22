"""RealOpen-AI voice pipeline.

Voice is another input/output modality for the EXISTING assistant — not a
separate assistant. This package implements the real-time WebSocket voice
pipeline around the existing agent loop:

    browser mic PCM ──▶ server AEC (exact far-end reference)
                     ──▶ VAD
                     ──▶ streaming ASR (Qwen3-ASR)
                     ──▶ run_agent_stream (the EXISTING agent loop)
                     ──▶ streamed assistant events (verbatim)
                     ──▶ sentence-chunked streaming TTS (Pocket TTS)
                     ──▶ PCM audio frames back to the browser

Provider/model selection comes from profiles.yml (single source of truth,
resolved via ``settings.get_voice_config()``); runtime behavior comes from
Settings (VOICE_* fields).

Submodules:
    audio          PCM conversion + rolling pre-roll ring buffer
    state          deterministic voice session state machine
    vad            voice-activity detection (webrtcvad / energy fallback)
    aec            acoustic echo cancellation (WebRTC / NLMS fallback)
    asr            streaming speech recognition (Qwen3-ASR)
    tts            streaming speech synthesis (Pocket TTS) + chunker
    models_store   install/validation/manifest for voice model assets
    manager        voice session registry (one session per conversation)
    session        the protocol-v1 WebSocket session implementation
"""

# Voice WebSocket protocol v1 constants (shared by session + tests).
PROTOCOL_VERSION = 1

# Binary frame prefixes (first byte of every binary WebSocket message).
FRAME_MIC = 0x01  # client → server: mic PCM s16le 16k mono
FRAME_FAR = 0x02  # client → server: far-end (played) PCM s16le 16k mono
FRAME_TTS = 0x03  # server → client: TTS PCM s16le 24k mono


def __version__() -> str:
    """Voice pipeline protocol version implemented by this package."""
    return str(PROTOCOL_VERSION)
