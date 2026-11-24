"""How Icarus's voice is delivered: chunking, pauses, pace, and the 'machine' room it plays through.

The goal is a voice that sounds like it comes out of the machine, not one pasted over it:
- one continuous stream per reply, so there is never a drop to digital silence between lines;
- pauses chosen from the text, not a fixed sleep plus whatever padding the engine left;
- a subtle room (body EQ, early reflections, a short tail, a faint noise floor) that keeps
  ringing into the pauses, rendered in stereo so it sits outside your head on headphones.
"""

import zlib

import numpy as np

# ── text → chunks, pace and pauses ──────────────────────────────────────────

FIRST_CHUNK_CHARS = 120   # the first chunk stays short so he starts talking quickly
CHUNK_CHARS = 300         # later chunks synthesize while the previous one plays


def pace(text: str) -> float:
    """Speed multiplier: brisk for short confirmations, unhurried for explanations."""
    n = len(text)
    if n <= 40:
        return 1.1
    if n <= 90:
        return 1.04
    return 1.0


def gap_before(prev: str | None, text: str, new_beat: bool = False) -> float:
    """Silence before a chunk, from how the previous one ended. Engine padding is trimmed first,
    so this is the whole pause. A little deterministic variation keeps it from sounding metered."""
    if not prev:
        return 0.0
    end = prev.rstrip()[-1:]
    base = {"?": .46, "!": .4, ".": .4, ":": .3, ";": .3, ",": .2}.get(end, .3)
    if new_beat:
        base += .22  # moving to the next point on the board
    jitter = (zlib.crc32(text.encode()) % 1000) / 1000 - .5
    return round(base + jitter * .1, 3)


# ── audio helpers ───────────────────────────────────────────────────────────

def trim_silence(audio: np.ndarray, rate: int, threshold: float = .008,
                 lead: float = .02, tail: float = .06) -> np.ndarray:
    """Cut the engine's leading/trailing silence (Kokoro leaves up to 0.7 s), keeping a short
    pad and fading the edges so the cut can't click."""
    loud = np.nonzero(np.abs(audio) > threshold)[0]
    if loud.size == 0:
        return audio[:0]
    a = max(0, loud[0] - int(lead * rate))
    b = min(audio.size, loud[-1] + int(tail * rate))
    out = audio[a:b].astype(np.float32, copy=True)
    fade = min(int(.01 * rate), out.size // 2)
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    return out


def line_offsets(texts: list[str], audio: np.ndarray, rate: int) -> list[float]:
    """Where each line starts (seconds) inside one chunk spoken as a whole: proportional to
    its length, then snapped to the quietest moment nearby (the breath between sentences)."""
    if not texts:
        return []
    total = sum(len(t) + 1 for t in texts)
    dur = audio.size / rate
    win = max(1, int(.02 * rate))
    frames = audio[: audio.size // win * win].reshape(-1, win)
    energy = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) if frames.size else np.zeros(0)
    offsets, acc = [0.0], 0
    for t in texts[:-1]:
        acc += len(t) + 1
        guess = acc / total * dur
        lo, hi = int(max(0, guess - .35) * rate / win), int(min(dur, guess + .35) * rate / win)
        if hi > lo and energy.size:
            guess = (lo + int(np.argmin(energy[lo:hi]))) * win / rate
        offsets.append(max(offsets[-1], guess))
    return offsets


# ── the room ────────────────────────────────────────────────────────────────

def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    k = np.hanning(width + 2)[1:-1]
    return np.convolve(x, k / k.sum(), mode="same")


class MachineRoom:
    """Stateful mono → stereo chain, processed block by block on the playback stream:
    gentle compression, then one FIR per ear (body EQ, early reflections, a short diffuse tail),
    plus a faint noise floor that fades in when the stream opens and out when it closes.

    amount 0 is a plain dry voice on both ears; 1 is the designed 'subtle' level."""

    BLOCK = 1024
    FLOOR_DB = -64.0

    def __init__(self, rate: int, amount: float = 1.0, seed: int = 7):
        self.rate, self.amount = rate, max(0.0, min(1.0, amount))
        rng = np.random.default_rng(seed)
        self.ir = self._impulse(rng)
        n = self.BLOCK + self.ir.shape[1] - 1
        self.nfft = 1 << (n - 1).bit_length()
        self.ir_f = np.fft.rfft(self.ir, self.nfft, axis=1)
        self.carry = np.zeros((2, self.nfft - self.BLOCK))
        self.latency = self._eq_taps // 2 / rate if self.amount else 0.0
        self.floor = self._noise(rng)
        self.floor_pos = 0
        self.floor_gain = 0.0
        self.floor_target = 0.0
        self.comp_gain = 1.0

    # design

    _eq_taps = 1025

    def _eq(self) -> np.ndarray:
        """Linear-phase body EQ: roll off rumble, a little low-mid 'box', soften the top."""
        n = 8192
        f = np.fft.rfftfreq(n, 1 / self.rate)
        f[0] = 1.0
        db = (10 * np.log10(1 / (1 + (85 / f) ** 4))           # high-pass ~85 Hz
              + 2.5 * np.exp(-np.log2(f / 280) ** 2 / .5)       # +2.5 dB body around 280 Hz
              - 1.0 * np.exp(-np.log2(f / 3200) ** 2 / .3)      # slight dip in the harsh band
              - 3.0 / (1 + (9000 / f) ** 4))                     # -3 dB shelf above ~9 kHz
        h = np.fft.irfft(10 ** (db * self.amount / 20), n)
        h = np.roll(h, self._eq_taps // 2)[: self._eq_taps] * np.hanning(self._eq_taps)
        return h

    def _impulse(self, rng) -> np.ndarray:
        if self.amount <= 0:
            return np.ones((2, 1))
        r = self.rate
        length = int(.3 * r)
        t = np.arange(length) / r
        ears = []
        for _ in range(2):  # different reflections per ear: that's what places the voice outside your head
            refl = np.zeros(length)
            for when in np.sort(rng.uniform(.005, .036, 9)):
                refl[int(when * r)] += .17 * np.exp(-when / .02) * rng.choice((-1, 1))
            refl = _smooth(refl, max(2, r // 4000))          # surfaces absorb the highs
            tail = rng.standard_normal(length) * np.exp(-6.9 * t / .22) * (t > .014)
            tail = _smooth(tail, max(2, r // 2500)) * .028
            ir = (refl + tail) * self.amount
            ir[0] += 1.0                                       # the direct voice, centred
            ears.append(np.convolve(self._eq(), ir))
        ir = np.array(ears)
        # Unity gain at 1 kHz so the room doesn't change loudness.
        g = np.abs(np.fft.rfft(ir[0], 1 << 15))[int(1000 * (1 << 15) / r)]
        return ir / g

    def _noise(self, rng) -> np.ndarray:
        """Six seconds of soft pink-ish stereo floor, band-limited, looped."""
        n = 6 * self.rate
        f = np.fft.rfftfreq(n, 1 / self.rate)
        f[0] = 1.0
        shape = 1 / np.sqrt(f) / (1 + (90 / f) ** 2) / (1 + (f / 5000) ** 2)
        noise = np.array([np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n) for _ in range(2)])
        noise /= np.sqrt((noise ** 2).mean())
        return noise * 10 ** (self.FLOOR_DB / 20) * self.amount

    # runtime

    def floor_on(self):
        self.floor_target = 1.0

    def floor_off(self):
        self.floor_target = 0.0

    @property
    def quiet(self) -> bool:
        return self.floor_gain == 0.0 and not np.any(np.abs(self.carry) > 1e-4)

    def _compress(self, x: np.ndarray) -> np.ndarray:
        """Gentle 2:1-ish leveling on 5 ms windows, smoothed: evens out the delivery like a device would."""
        if self.amount <= 0:
            return x
        sub = 128
        ratio = 1 + 1.1 * self.amount
        thresh = -24.0
        rms = np.sqrt((x.reshape(-1, sub) ** 2).mean(axis=1) + 1e-12)
        level = 20 * np.log10(rms)
        target = 10 ** (-np.maximum(0, level - thresh) * (1 - 1 / ratio) / 20) * 10 ** (2 * self.amount / 20)
        gains = np.empty_like(target)
        g = self.comp_gain
        for i, tg in enumerate(target):
            g += (tg - g) * (.5 if tg < g else .06)
            gains[i] = g
        self.comp_gain = g
        return x * np.repeat(gains, sub)

    def process(self, mono: np.ndarray) -> np.ndarray:
        """One BLOCK of mono float32 → (2, BLOCK) stereo float32."""
        x = self._compress(mono.astype(np.float64))
        y = np.fft.irfft(np.fft.rfft(x, self.nfft) * self.ir_f, self.nfft, axis=1)
        y[:, : self.carry.shape[1]] += self.carry
        out, self.carry = y[:, : self.BLOCK], y[:, self.BLOCK:].copy()
        if self.floor_gain or self.floor_target:
            step = self.BLOCK / (.3 * self.rate)  # 300 ms fades
            delta = self.floor_target - self.floor_gain
            nxt = self.floor_gain + max(-step, min(step, delta))
            ramp = np.linspace(self.floor_gain, nxt, self.BLOCK)
            idx = (self.floor_pos + np.arange(self.BLOCK)) % self.floor.shape[1]
            out = out + self.floor[:, idx] * ramp
            self.floor_pos = int(idx[-1] + 1)
            self.floor_gain = float(nxt)
        # Soft ceiling instead of hard clipping.
        a = np.abs(out)
        out = np.where(a < .8, out, np.sign(out) * (.8 + .18 * np.tanh((a - .8) / .18)))
        return out.astype(np.float32)

    def render(self, mono: np.ndarray, tail: float = .5) -> np.ndarray:
        """Offline: process a whole mono signal (plus a ring-out tail) → (2, n). Used for previews."""
        pad = (-(mono.size + int(tail * self.rate))) % self.BLOCK + int(tail * self.rate)
        x = np.concatenate([mono, np.zeros(pad, np.float32)])
        self.floor_on()
        blocks = []
        for i in range(0, x.size, self.BLOCK):
            if i >= mono.size:
                self.floor_off()
            blocks.append(self.process(x[i:i + self.BLOCK]))
        return np.concatenate(blocks, axis=1)
