"""Settings for the desktop daemon, read from the repo's .env and the environment."""

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_DIR = REPO_ROOT / "desktop"


def load_env() -> None:
    """Load REPO_ROOT/.env into os.environ without overriding what's already set."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip().rstrip("\r")
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


BASE_VOCAB = ("Icarus, Councilor, Claude, Claude Code, subagent, delegate, Hyprland, kitty, Firefox, Spotify, "
              "Discord, Gmail, Telegram, worktree, llama-server, Qwen, Kokoro")


def whisper_vocab() -> str:
    """Words Whisper should expect: the base list, your Claude projects, and DESKTOP_WHISPER_VOCAB."""
    words = [BASE_VOCAB]
    projects = [p.split("=", 1)[0].strip() for p in os.getenv("DESKTOP_CLAUDE_PROJECTS", "").split(",") if "=" in p]
    if projects:
        words.append(", ".join(projects))
    extra = os.getenv("DESKTOP_WHISPER_VOCAB", "").strip()
    if extra:
        words.append(extra)
    return ", ".join(words) + "."


def runtime_dir() -> Path:
    base = os.getenv("XDG_RUNTIME_DIR") or f"/tmp/icarus-desktop-{os.getuid()}"
    return Path(base) / "icarus-desktop"


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _hours(value: str) -> tuple[int, int] | None:
    """'23-8' -> (23, 8)."""
    try:
        start, end = (int(x) % 24 for x in value.split("-"))
        return start, end
    except ValueError:
        return None


@dataclass
class Config:
    api_url: str = "ws://127.0.0.1:8000/ws/desktop"
    token: str = ""
    ctl_socket: Path = field(default_factory=lambda: runtime_dir() / "ctl.sock")
    renderer_socket: Path = field(default_factory=lambda: runtime_dir() / "renderer.sock")
    renderer_cmd: list[str] = field(default_factory=list)
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"
    whisper_beam: int = 5
    whisper_threads: int = max(4, min(12, (os.cpu_count() or 8) // 2))
    whisper_vocab: str = ""              # names to bias recognition toward (see load_config)
    tts_engine: str = "piper"            # piper | kokoro
    piper_voice: Path = DESKTOP_DIR / "voices" / "en_US-ryan-high.onnx"
    kokoro_voice: str = "am_onyx"
    kokoro_model: Path = DESKTOP_DIR / "voices" / "kokoro-v1.0.onnx"
    kokoro_voices: Path = DESKTOP_DIR / "voices" / "voices-v1.0.bin"
    voice_speed: float = 1.0
    voice_fx: float = 0.0                # 0..1 'construct' processing
    voice_room: float = 1.0              # 0..1 the 'machine' room he plays through; 0 is dry
    linger_seconds: float = 6.0
    followup_seconds: float = 8.0       # mic stays open this long after he answers; 0 disables
    max_record_seconds: float = 30.0
    end_silence_seconds: float = 1.5     # silence after speech that ends a recording; 0 disables
    action_timeout: float = 12.0
    board_port: int = 47811                      # localhost page sync for popped-out boards
    quiet_hours: tuple[int, int] | None = None   # (start_hour, end_hour): no spoken pop-ups
    announce_max_age: float = 3 * 3600           # held pop-ups older than this are dropped (Discord has them)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_market: str = "US"
    app_denylist: tuple[str, ...] = ()


def load_config() -> Config:
    load_env()
    renderer = os.getenv("DESKTOP_RENDERER_CMD", "").split()
    if not renderer:
        renderer = ["/usr/bin/python3", str(DESKTOP_DIR / "renderer" / "avatar.py")]
        output = os.getenv("DESKTOP_OUTPUT", "").strip()
        monitor = os.getenv("DESKTOP_MONITOR", "").strip()
        if output:
            renderer += ["--output", output]
        elif monitor:
            renderer += ["--monitor", monitor]
    return Config(
        api_url=os.getenv("DESKTOP_API_URL", Config.api_url),
        token=os.getenv("DESKTOP_TOKEN", ""),
        renderer_cmd=renderer,
        whisper_model=os.getenv("DESKTOP_WHISPER_MODEL", Config.whisper_model),
        whisper_device=os.getenv("DESKTOP_WHISPER_DEVICE", Config.whisper_device),
        whisper_compute=os.getenv("DESKTOP_WHISPER_COMPUTE", Config.whisper_compute),
        whisper_beam=int(_float("DESKTOP_WHISPER_BEAM", Config.whisper_beam)),
        whisper_threads=int(_float("DESKTOP_WHISPER_THREADS", Config.whisper_threads)),
        whisper_vocab=whisper_vocab(),
        tts_engine=os.getenv("DESKTOP_TTS_ENGINE", Config.tts_engine).lower(),
        piper_voice=Path(os.getenv("DESKTOP_PIPER_VOICE", str(Config.piper_voice))),
        kokoro_voice=os.getenv("DESKTOP_KOKORO_VOICE", Config.kokoro_voice),
        voice_speed=_float("DESKTOP_VOICE_SPEED", Config.voice_speed),
        voice_fx=max(0.0, min(1.0, _float("DESKTOP_VOICE_FX", Config.voice_fx))),
        voice_room=max(0.0, min(1.0, _float("DESKTOP_VOICE_ROOM", Config.voice_room))),
        linger_seconds=_float("DESKTOP_LINGER_SECONDS", Config.linger_seconds),
        followup_seconds=_float("DESKTOP_FOLLOWUP_SECONDS", Config.followup_seconds),
        max_record_seconds=_float("DESKTOP_MAX_RECORD_SECONDS", Config.max_record_seconds),
        end_silence_seconds=_float("DESKTOP_END_SILENCE_SECONDS", Config.end_silence_seconds),
        action_timeout=_float("DESKTOP_ACTION_TIMEOUT_SECONDS", Config.action_timeout),
        quiet_hours=_hours(os.getenv("DESKTOP_QUIET_HOURS", "")),
        board_port=int(_float("DESKTOP_BOARD_PORT", Config.board_port)),
        announce_max_age=_float("DESKTOP_ANNOUNCE_MAX_AGE_HOURS", 3) * 3600,
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        spotify_market=os.getenv("SPOTIFY_MARKET", "US"),
        app_denylist=tuple(x.strip().lower() for x in os.getenv("DESKTOP_APP_DENYLIST", "").split(",") if x.strip()),
    )
