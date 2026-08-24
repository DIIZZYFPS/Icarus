"""
model_bank.py — Resolves the local model's usable context length.

Primary source: live discovery via llama-server's own /props endpoint — same
"ask the server, don't trust a static value" principle routes/dashboard.py
already uses for the model's display name (see its _get_model_display_name,
which reads /v1/models rather than trusting LOCAL_LLM_MODEL). /props is
llama.cpp's native route, not part of the OpenAI-compatible surface, so it
lives one level above LOCAL_LLM_URL (which already has /v1 appended).

Falls back to MODEL_CONTEXT_LENGTHS below when live discovery fails (older
server build without /props, unreachable, model swapped mid-request), keyed
by the same basename-without-extension /v1/models already yields. Falls back
again to DEFAULT_CONTEXT_LENGTH if neither resolves.

Cached in-process after first successful resolution — the model doesn't
hot-swap while a container is running, so there's no reason to hit the
network on every call site that needs this (memory_repo's should_compact
runs on every single append_memory call).
"""

import os
import logging

logger = logging.getLogger(__name__)

# Manually-curated fallback for when live /props discovery isn't available.
# Fill in with n_ctx as actually configured for that GGUF on the host
# (llama-server's --ctx-size flag) — that's very likely smaller than the
# model's architectural max once VRAM budget is accounted for, so don't
# guess it from the model card. Left empty until confirmed: a wrong number
# here is worse than falling through to DEFAULT_CONTEXT_LENGTH, which is at
# least honestly conservative.
MODEL_CONTEXT_LENGTHS: dict[str, int] = {
    # "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL": <fill in the actual --ctx-size here>,
}

DEFAULT_CONTEXT_LENGTH = 8192  # conservative fallback if nothing above resolves

_cached_context_length: int | None = None


def _props_url() -> str | None:
    base_url = os.getenv("LOCAL_LLM_URL", "").rstrip("/")
    if not base_url:
        return None
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return f"{base_url}/props"


def _models_url() -> str | None:
    base_url = os.getenv("LOCAL_LLM_URL", "").rstrip("/")
    return f"{base_url}/models" if base_url else None


async def _fetch_live_context_length() -> int | None:
    """Ask llama-server directly via /props. Its n_ctx has moved around
    between llama.cpp versions (top-level vs. nested under
    default_generation_settings), so check both."""
    import httpx

    url = _props_url()
    if url is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        n_ctx = data.get("n_ctx") or data.get("default_generation_settings", {}).get("n_ctx")
        return int(n_ctx) if n_ctx else None
    except Exception as e:
        logger.warning(f"[model_bank] Live context-length discovery failed: {e}")
        return None


async def _fetch_model_name() -> str | None:
    """Same derivation as routes/dashboard.py:_get_model_display_name,
    duplicated rather than imported — same reasoning as telemetry_tools.py's
    duplicated Redis keys: this stays a thin, independent reader with no
    coupling to a routes module."""
    import httpx

    url = _models_url()
    if url is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        model_id = data["data"][0]["id"]
        name = os.path.basename(model_id)
        if name.lower().endswith(".gguf"):
            name = name[:-5]
        return name or None
    except Exception as e:
        logger.warning(f"[model_bank] Live model-name discovery failed: {e}")
        return None


async def get_model_context_length(force_refresh: bool = False) -> int:
    """Resolve the local model's usable context length: live /props
    discovery first, then MODEL_CONTEXT_LENGTHS keyed by model name, then
    DEFAULT_CONTEXT_LENGTH. Cached after first resolution for the life of
    the process — pass force_refresh=True to bypass that (e.g. after a
    known model swap without a restart)."""
    global _cached_context_length
    if _cached_context_length is not None and not force_refresh:
        return _cached_context_length

    n_ctx = await _fetch_live_context_length()
    if n_ctx:
        logger.info(f"[model_bank] Resolved context length live via /props: {n_ctx}")
        _cached_context_length = n_ctx
        return n_ctx

    model_name = await _fetch_model_name()
    if model_name and model_name in MODEL_CONTEXT_LENGTHS:
        n_ctx = MODEL_CONTEXT_LENGTHS[model_name]
        logger.info(f"[model_bank] Resolved context length from model bank ({model_name}): {n_ctx}")
        _cached_context_length = n_ctx
        return n_ctx

    logger.warning(
        f"[model_bank] Could not resolve context length live or from the model "
        f"bank (model={model_name!r}); falling back to DEFAULT_CONTEXT_LENGTH="
        f"{DEFAULT_CONTEXT_LENGTH}"
    )
    _cached_context_length = DEFAULT_CONTEXT_LENGTH
    return DEFAULT_CONTEXT_LENGTH
