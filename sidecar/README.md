# Telemetry Sidecar (C++)

A lightweight C++ process that samples host resources and publishes JSON telemetry to Redis for Icarus ingestion.

## Signals emitted
- cpu_percent
- memory_used_mb
- memory_total_mb
- disk_used_percent
- gpu_util_percent (optional, collected with nvidia-smi if available)

## Redis target
- List key: icarus:metrics:sidecar
- Message format: JSON object with timestamp, source, and metrics block

## Build (Windows, MSVC)
```powershell
cd sidecar
cl /std:c++17 /EHsc telemetry_sidecar.cpp /link Ws2_32.lib
```

## Build (Windows, MinGW g++)
```powershell
cd sidecar
g++ -std=gnu++14 -O2 telemetry_sidecar.cpp -o telemetry_sidecar.exe -lws2_32
```

## Build (Linux/macOS, g++)
```bash
cd sidecar
g++ -std=c++17 -O2 telemetry_sidecar.cpp -o telemetry_sidecar
```

## Run
Set optional environment variables:
- ICARUS_REDIS_HOST (default 127.0.0.1)
- ICARUS_REDIS_PORT (default 6379)
- ICARUS_REDIS_KEY (default icarus:metrics:sidecar)
- ICARUS_INTERVAL_MS (default 2000)
- ICARUS_SOURCE (default local-sidecar)
- ICARUS_ENABLE_GPU (default 0 on Windows, 1 on Linux/macOS)

Then run the binary. It prints each payload to stdout and pushes it to Redis.
