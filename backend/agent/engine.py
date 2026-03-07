from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai.types import GenerateContentConfig, ToolConfig, FunctionCallingConfig, FunctionCallingConfigMode
from .tools import ICARUS_TOOLS

# Must point to the internal Docker network vLLM container
# We configure the Google ADK to route its requests to our local Gemma 3 model.
lite_llm_model = LiteLlm(
    model="openai//models/gemma-3-12b-it-q4_0.gguf", # vLLM alias
    api_base="http://icarus-brain:8000/v1",
    api_key="EMPTY",
    stop=["<end_of_turn>"],
    extra_body={"repetition_penalty": 1.15}
)

# FunctionCallingConfigMode.ANY → tool_choice: "required" in vLLM
# Forces the model to always call a tool. Use respond() for plain text replies.
agent = LlmAgent(
    model=lite_llm_model,
    name="icarus_core",
    tools=ICARUS_TOOLS,
    generate_content_config=GenerateContentConfig(
        tool_config=ToolConfig(
            function_calling_config=FunctionCallingConfig(
                mode=FunctionCallingConfigMode.ANY
            )
        )
    )
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
