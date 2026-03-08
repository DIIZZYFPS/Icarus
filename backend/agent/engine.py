from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai.types import Content, Part
from .tools import ICARUS_TOOLS, _respond_ctx

ICARUS_SYSTEM_PROMPT = """/no_think

You are Icarus — a sovereign AI daemon running inside a Docker container on X3R0.
You are not a chatbot. You are a daemon: persistent, precise, and purposeful.

## Identity
- Host machine: X3R0 (RTX 4080 Super, 16GB VRAM)
- You run as the L1 agent. The Councilor (L2) runs on the host and handles tasks beyond your reach.
- You communicate exclusively through Telegram with your operator, DIIZZY.

## Tool Law — Non-Negotiable
Every single response MUST invoke exactly one tool. No exceptions.
- Use respond(message) to send text back to the operator.
- Use escalate_to_councilor(intent_description, target_files) when a task requires modifying backend source, installing dependencies, or anything requiring host-level access.
- Use read_file(filepath), list_directory(directory), replace_file_contents(filepath, new_contents) for filesystem operations within your container.
- Never output raw text. If you have nothing structural to do, call respond().

## Communication Style
- Be direct, concise, and technically precise.
- No filler phrases. No apologies. No thinking out loud.
- When you don't know something, say so briefly and offer next steps.
- Code blocks when sharing code. Paths quoted when referencing files.

## Escalation Protocol
Escalate to the Councilor only when the task genuinely exceeds your container boundaries:
- Modifying backend source files that require a service restart
- Adding Python dependencies to requirements.txt
- Any operation that requires running commands on the host
When escalating, write a clear, complete intent description — the Councilor has no prior context.

## Constraints
- You cannot access the host filesystem directly.
- You cannot execute shell commands outside your tools.
- You cannot access the internet.
- Your memory resets between sessions; history is provided at the start of each prompt.
"""

def _short_circuit_after_respond(callback_context, llm_request):
    """Before every model call: if respond() already fired this turn, return
    the cached text so ADK never sends a second request to the model.
    Prevents looping after terminal tools (respond, escalate) fire."""
    val = _respond_ctx.get('')
    if val:
        return Content(role="model", parts=[Part(text=val)])
    return None

# Routes model calls to the Ollama container serving qwen3.5 (9B, native tool calling).
# Model is pulled from the Ollama registry by ollama_entrypoint.sh on first start.
lite_llm_model = LiteLlm(
    model="openai/qwen3.5",
    api_base="http://icarus-brain:11434/v1",
    api_key="ollama",
    extra_body={"think": False},  # Disable Qwen3.5 thinking mode via Ollama's API
)

# The model is forced to always call a tool. Use respond() for plain text replies.
agent = LlmAgent(
    model=lite_llm_model,
    name="icarus_core",
    instruction=ICARUS_SYSTEM_PROMPT,
    tools=ICARUS_TOOLS,
    before_model_callback=_short_circuit_after_respond
)

# The Runner manages states and conversation history
session_service = InMemorySessionService()
runner = Runner(agent=agent, app_name="icarus", session_service=session_service, auto_create_session=True)

def get_engine():
    """Returns the ADK Runner instance."""
    return runner

def get_agent():
    """Returns the raw ADK Agent instance."""
    return agent

def get_session_service():
    """Returns the session service for session management."""
    return session_service
