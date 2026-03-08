#!/bin/bash
set -e

# Start Ollama server in background
ollama serve &
SERVE_PID=$!

# Wait for server to be ready
echo "[entrypoint] Waiting for Ollama to start..."
until bash -c '</dev/tcp/localhost/11434' 2>/dev/null; do
    sleep 1
done
echo "[entrypoint] Ollama is ready."

# Pull qwen3.5 from Ollama registry if not already present (one-time, ~6.6 GB)
if ollama list | grep -q "qwen3.5"; then
    echo "[entrypoint] Model qwen3.5 already exists, skipping pull."
else
    echo "[entrypoint] Pulling qwen3.5 from registry (one-time, ~6.6 GB)..."
    ollama pull qwen3.5
    echo "[entrypoint] Pull complete."
fi

# Hand off to the background serve process
wait $SERVE_PID
