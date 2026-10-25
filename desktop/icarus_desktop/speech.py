"""Local speech: faster-whisper for speech-to-text, Piper for the voice. Both load once."""

import asyncio
import logging
import re
import threading

import numpy as np

from .audio import MIC_RATE

logger = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self, model: str, device: str, compute_type: str):
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                try:
                    self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
                except Exception as e:
                    if self.device == "cpu":
                        raise
                    logger.warning(f"[stt] {self.device} unavailable ({e}); falling back to CPU")
                    self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            return self._model

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._load)
        logger.info(f"[stt] {self.model_name} ready")

    def _transcribe(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32) / 32768.0
        if audio.size < MIC_RATE * .3:
            return ""
        segments, _ = self._load().transcribe(audio, language="en", beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm: bytes) -> str:
        return await asyncio.to_thread(self._transcribe, pcm)


class TextToSpeech:
    def __init__(self, voice_path):
        self.voice_path = voice_path
        self._voice = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice
                self._voice = PiperVoice.load(str(self.voice_path))
            return self._voice

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._load)
        logger.info(f"[tts] voice {self.voice_path.name} ready")

    def _synth(self, text: str) -> tuple[bytes, int]:
        chunks = list(self._load().synthesize(text))
        if not chunks:
            return b"", 22050
        return b"".join(c.audio_int16_bytes for c in chunks), chunks[0].sample_rate

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        return await asyncio.to_thread(self._synth, text)


_URL = re.compile(r"https?://\S+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(])")


def speakable(text: str) -> str:
    """Strip markdown and links the model may have produced despite the voice-mode prompt."""
    text = re.sub(r"```.*?```", " (code on screen) ", text, flags=re.S)
    text = _URL.sub("the link", text)
    text = re.sub(r"[*_#`>|~]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[str]:
    """Split into sentences for one-at-a-time synthesis and subtitles; merge tiny fragments."""
    parts = [p.strip() for p in _SENTENCE.split(speakable(text)) if p.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) < 18:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged
