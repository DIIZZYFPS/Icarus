# Icarus
A branched version of Project-Icarus to serve a possible more advanced purpose

## C++ Telemetry Sidecar MVP

This repo now includes a lightweight C++ sidecar that samples host resources and publishes telemetry to Redis. The backend consumes this stream, computes a simulation readiness signal, and exposes recent summaries.

### Added pieces
- Sidecar source: `sidecar/telemetry_sidecar.cpp`
- Sidecar docs: `sidecar/README.md`
- Backend consumer: `backend/agent/metrics_consumer.py`
- Demo tail script: `backend/scripts/tail_metrics.py`
- API endpoints: `GET /metrics/latest` and `GET /metrics/recent`

### Fast demo flow
1. Start Redis + API with Docker Compose.
2. Build and run the sidecar from `sidecar/`.
3. Watch readiness logs from API output.
4. Optional: run `python backend/scripts/tail_metrics.py` to stream compact terminal output.
