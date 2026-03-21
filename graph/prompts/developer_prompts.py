"""Developer agent prompts — loaded from software_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("software_engineer")

PLAN_SYSTEM_PROMPT: str = _p["plan"]["system"]
GENERATE_SYSTEM_PROMPT: str = _p["generate"]["system"]
FILE_CONTEXT: dict[str, str] = _p["file_context"]
ROUTER_HINT: str = _p["router_hint"]
SERVICE_HINT: str = _p["service_hint"]
COMPONENT_HINT: str = _p["component_hint"]
