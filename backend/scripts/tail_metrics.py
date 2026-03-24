import asyncio
import json
import os

import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SUMMARY_KEY = "icarus:metrics:summary"


async def main():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    print(f"[tail_metrics] Connected to {REDIS_URL}")
    print("[tail_metrics] Press Ctrl+C to stop.")

    seen = set()
    try:
        while True:
            rows = await client.lrange(SUMMARY_KEY, 0, 4)
            for row in reversed(rows):
                if row in seen:
                    continue
                seen.add(row)
                try:
                    m = json.loads(row)
                except Exception:
                    continue

                ts = m.get("timestamp", "?")
                src = m.get("source", "?")
                ready = m.get("simulation_readiness", "?")
                score = m.get("pressure_score", 0.0)
                cpu = m.get("cpu_percent", 0.0)
                mem_used = m.get("memory_used_mb", 0.0)
                mem_total = m.get("memory_total_mb", 1.0)
                disk = m.get("disk_used_percent", 0.0)

                print(
                    f"[{ts}] src={src} readiness={ready} score={score:.3f} "
                    f"cpu={cpu:.1f}% mem={mem_used:.0f}/{mem_total:.0f}MB disk={disk:.1f}%"
                )

            if len(seen) > 2000:
                seen.clear()

            await asyncio.sleep(2)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
