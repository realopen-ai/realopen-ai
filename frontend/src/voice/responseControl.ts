export type ResponseVoiceState =
  | "inactive"
  | "connecting"
  | "listening"
  | "processing"
  | "speaking"
  | "interrupting"
  | "stopping"
  | "error";

/** Muting is a hard gate before both browser VAD and ASR transmission. */
export function shouldCaptureMicrophone(isMicMuted: boolean): boolean {
  return !isMicMuted;
}

/** Select the transport controlled by the shared right-edge stop button. */
export function responseTransportForStop(
  voiceState: ResponseVoiceState,
): "voice" | "text" {
  return voiceState === "processing" ||
    voiceState === "speaking" ||
    voiceState === "interrupting"
    ? "voice"
    : "text";
}
