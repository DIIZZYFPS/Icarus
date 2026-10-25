"""Local speech: faster-whisper for speech-to-text, Piper for the voice. Both load once."""

import asyncio
import logging
import re
import threading

import numpy as np

from .audio import MIC_RATE

logger = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self, model: str, device: str, compute_type: str, beam: int = 5, threads: int = 8, vocab: str = ""):
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self.beam, self.threads, self.vocab = beam, threads, vocab
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                try:
                    self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type,
                                               cpu_threads=self.threads)
                except Exception as e:
                    if self.device == "cpu":
                        raise
                    logger.warning(f"[stt] {self.device} unavailable ({e}); falling back to CPU")
                    self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", cpu_threads=self.threads)
            return self._model

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._load)
        logger.info(f"[stt] {self.model_name} ready")

    def _transcribe(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32) / 32768.0
        if audio.size < MIC_RATE * .3:
            return ""
        # The vocabulary prompt biases recognition toward names Icarus actually hears
        # ("Councilor", not "counselor"; "Hyprland", not "Hyphen").
        segments, _ = self._load().transcribe(audio, language="en", beam_size=self.beam, vad_filter=True,
                                              initial_prompt=self.vocab or None)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm: bytes) -> str:
        return await asyncio.to_thread(self._transcribe, pcm)


def construct_fx(audio: np.ndarray, rate: int, amount: float) -> np.ndarray:
    """Optional 'construct' character: a short modulated chorus, a faint 32 Hz ring mod and a
    tilt toward the highs. amount 0 leaves the voice untouched, 1 is the audition's processed sample."""
    if amount <= 0 or audio.size == 0:
        return audio
    n = np.arange(audio.size)
    t = n / rate
    delay = (0.011 + 0.003 * np.sin(2 * np.pi * .7 * t)) * rate
    idx = np.clip(n - delay, 0, audio.size - 1)
    lo = idx.astype(int)
    frac = idx - lo
    delayed = audio[lo] * (1 - frac) + audio[np.minimum(lo + 1, audio.size - 1)] * frac
    wet = audio + .38 * delayed
    wet = wet * (.9 + .1 * np.sin(2 * np.pi * 32 * t))
    wet = np.append(wet[:1], wet[1:] - .82 * wet[:-1]) * .55 + wet * .45
    out = audio * (1 - amount) + wet * amount
    peak = float(np.abs(out).max()) or 1.0
    return (out / peak * min(.95, max(peak, float(np.abs(audio).max())))).astype(np.float32)


class TextToSpeech:
    """Piper (light, fast) or Kokoro (more natural); chosen by DESKTOP_TTS_ENGINE."""

    def __init__(self, config):
        self.engine = config.tts_engine
        self.config = config
        self._voice = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"kokoro/{self.config.kokoro_voice}" if self.engine == "kokoro" else self.config.piper_voice.name

    def _load(self):
        with self._lock:
            if self._voice is None:
                if self.engine == "kokoro":
                    from kokoro_onnx import Kokoro
                    self._voice = Kokoro(str(self.config.kokoro_model), str(self.config.kokoro_voices))
                else:
                    from piper import PiperVoice
                    self._voice = PiperVoice.load(str(self.config.piper_voice))
            return self._voice

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._synth, "Ready.")
        logger.info(f"[tts] voice {self.label} ready")

    def _synth(self, text: str) -> tuple[bytes, int]:
        voice = self._load()
        if self.engine == "kokoro":
            name = self.config.kokoro_voice
            audio, rate = voice.create(text, voice=name, speed=self.config.voice_speed,
                                       lang="en-gb" if name.startswith("b") else "en-us")
            audio = np.asarray(audio, np.float32)
        else:
            chunks = list(voice.synthesize(text))
            if not chunks:
                return b"", 22050
            rate = chunks[0].sample_rate
            audio = np.frombuffer(b"".join(c.audio_int16_bytes for c in chunks), np.int16).astype(np.float32) / 32768.0
        audio = construct_fx(audio, rate, self.config.voice_fx)
        return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes(), rate

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
