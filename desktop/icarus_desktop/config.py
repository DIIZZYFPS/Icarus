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


def runtime_dir() -> Path:
    base = os.getenv("XDG_RUNTIME_DIR") or f"/tmp/icarus-desktop-{os.getuid()}"
    return Path(base) / "icarus-desktop"


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    api_url: str = "ws://127.0.0.1:8000/ws/desktop"
    token: str = ""
    ctl_socket: Path = field(default_factory=lambda: runtime_dir() / "ctl.sock")
    renderer_socket: Path = field(default_factory=lambda: runtime_dir() / "renderer.sock")
    renderer_cmd: list[str] = field(default_factory=list)
    whisper_model: str = "small.en"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"
    piper_voice: Path = DESKTOP_DIR / "voices" / "en_US-ryan-high.onnx"
    linger_seconds: float = 6.0
    max_record_seconds: float = 30.0
    end_silence_seconds: float = 1.5     # silence after speech that ends a recording; 0 disables
    action_timeout: float = 12.0
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
        piper_voice=Path(os.getenv("DESKTOP_PIPER_VOICE", str(Config.piper_voice))),
        linger_seconds=_float("DESKTOP_LINGER_SECONDS", Config.linger_seconds),
        max_record_seconds=_float("DESKTOP_MAX_RECORD_SECONDS", Config.max_record_seconds),
        end_silence_seconds=_float("DESKTOP_END_SILENCE_SECONDS", Config.end_silence_seconds),
        action_timeout=_float("DESKTOP_ACTION_TIMEOUT_SECONDS", Config.action_timeout),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        spotify_market=os.getenv("SPOTIFY_MARKET", "US"),
        app_denylist=tuple(x.strip().lower() for x in os.getenv("DESKTOP_APP_DENYLIST", "").split(",") if x.strip()),
    )
