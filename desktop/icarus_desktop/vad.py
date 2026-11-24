"""vad.py — deciding when someone is speaking, from the mic level alone."""


class EndOfSpeech:
    """Ends a recording after speech followed by `silence` seconds of quiet.

    The threshold adapts to the room: the first 300 ms set the noise floor, and
    speech is anything clearly above it."""

    def __init__(self, silence: float):
        self.silence = silence
        self.reset()

    def reset(self):
        self.t0 = None
        self.floor = []
        self.spoke = False
        self.quiet_since = None
        self.fired = False

    def preset(self, floor: float, now: float):
        """Carry on a recording that began before this detector: speech is already under way."""
        self.reset()
        self.t0, self.floor, self.spoke = now - 1.0, [floor], True

    def feed(self, level: float, now: float) -> bool:
        if self.silence <= 0 or self.fired:
            return False
        if self.t0 is None:
            self.t0 = now
        if now - self.t0 < .3:
            self.floor.append(level)
            return False
        floor = sum(self.floor) / len(self.floor) if self.floor else 0.0
        threshold = max(.012, floor * 3)
        if level > threshold:
            self.spoke = True
            self.quiet_since = None
            return False
        if not self.spoke:
            return False
        if self.quiet_since is None:
            self.quiet_since = now
        self.fired = now - self.quiet_since >= self.silence
        return self.fired


class BargeInDetector:
    """Spots someone starting to talk over him, from the mic level while he speaks.

    Meant for headphones, where the mic hears the room but not him. The first 300 ms set the
    noise floor. Speech is sustained sound clearly above it (the same bar as EndOfSpeech); a cough or a short "mm" is not,
    so it fires only after `onset` seconds of it, once."""

    FLOOR_SECONDS = .3
    GAP_SECONDS = .15      # a dip this short inside a word doesn't restart the count

    def __init__(self, onset: float = .3):
        self.onset = onset
        self.reset()

    def reset(self):
        self.t0 = None
        self.floor_levels = []
        self.loud_since = None
        self.last_loud = 0.0
        self.fired = False
        self.peak = 0.0

    @property
    def floor(self) -> float:
        return sum(self.floor_levels) / len(self.floor_levels) if self.floor_levels else 0.0

    def feed(self, level: float, now: float) -> bool:
        if self.fired:
            return False
        self.peak = max(self.peak, level)
        if self.t0 is None:
            self.t0 = now
        if now - self.t0 < self.FLOOR_SECONDS:
            self.floor_levels.append(level)
            return False
        if level > max(.012, self.floor * 3):
            if self.loud_since is None or now - self.last_loud > self.GAP_SECONDS:
                self.loud_since = now
            self.last_loud = now
            self.fired = now - self.loud_since >= self.onset
            return self.fired
        return False
