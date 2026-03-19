import asyncio
import json
import os
import time
from pathlib import Path

STREAM = "tasks:email_priority"
RESULT_KEY_PREFIX = "icarus:email_score:"


def _load_env_file(env_path: Path) -> None:
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

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        os.environ.setdefault(key, value)


async def main() -> int:
    try:
        import redis.asyncio as redis  # type: ignore
    except ModuleNotFoundError:
        print("Missing dependency: redis")
        print("Run either:")
        print("  pip install redis")
        print("or run the script in the API container:")
        print("  docker compose exec icarus-api python backend/scripts/smoke_test_email_worker.py")
        return 2

    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    _load_env_file(env_path)

    redis_url = (os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip()
    timeout_s = int((os.getenv("WORKER_SMOKE_TIMEOUT") or "30").strip())

    client = redis.from_url(redis_url, decode_responses=True)
    try:
        # Use urgency terms so the worker can score via heuristics without model calls.
        payload = {
            "subject": "URGENT worker smoke test",
            "body": "asap please review this smoke test task",
            "message_id": f"smoke-{int(time.time())}",
        }
        task_id = await client.xadd(STREAM, payload)
        print(f"Enqueued task_id={task_id} on stream={STREAM}")

        result_key = f"{RESULT_KEY_PREFIX}{task_id}"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            raw = await client.get(result_key)
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                print("Smoke test passed. Worker result:")
                print(json.dumps(parsed, indent=2))
                return 0
            await asyncio.sleep(1)

        print(
            f"Smoke test timed out after {timeout_s}s. "
            f"No result at key {result_key}."
        )
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
