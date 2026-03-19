import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/pubsub",
]


def _required_env(name: str, env_path: Path) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required env var: {name}. "
            f"Set it in your shell or in {env_path}."
        )
    return value


def _get_env_path() -> Path:
    # backend/scripts/generate_gmail_refresh_token.py -> repo root/.env
    return Path(__file__).resolve().parents[2] / ".env"


def _upsert_env_var(env_path: Path, key: str, value: str) -> None:
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            replaced = True
            break

    if not replaced:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_env_file(env_path: Path) -> None:
    """Load KEY=VALUE pairs from .env into process env (without overriding existing vars)."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        # Support common quoted .env values.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
            value = value[1:-1]

        os.environ.setdefault(key, value)


def _run_console_flow(client_id: str, client_secret: str) -> str:
    redirect_uri = (os.getenv("GMAIL_OAUTH_REDIRECT_URI") or "http://localhost:8080/").strip()
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        # oauthlib requires HTTPS by default; loopback HTTP is acceptable for local desktop-style OAuth.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    # Accept scope changes (Google may return additional granted scopes)
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri, "http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
    )

    print("\nOpen this URL in your browser and approve access:")
    print(auth_url)
    print("\nAfter approval, you will be redirected to localhost.")
    print("If the page fails to load, copy the FULL redirected URL from the browser address bar and paste it below.")
    print("\nPaste redirected URL:")
    authorization_response = input("> ").strip()

    flow.fetch_token(authorization_response=authorization_response)
    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        raise RuntimeError(
            "No refresh token was returned. Revoke prior app access and retry with prompt=consent."
        )
    return refresh_token


def main() -> None:
    env_path = _get_env_path()
    _load_env_file(env_path)

    client_id = _required_env("GMAIL_OAUTH_CLIENT_ID", env_path)
    client_secret = _required_env("GMAIL_OAUTH_CLIENT_SECRET", env_path)

    refresh_token = _run_console_flow(client_id, client_secret)

    _upsert_env_var(env_path, "GMAIL_OAUTH_REFRESH_TOKEN", refresh_token)

    print("\nSuccess: Gmail refresh token generated.")
    print(f"Saved GMAIL_OAUTH_REFRESH_TOKEN in: {env_path}")
    print("Restart icarus-api so it picks up the new env var.")


if __name__ == "__main__":
    main()
