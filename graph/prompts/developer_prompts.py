"""Developer agent prompts — loaded from software_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("software_engineer")

# ── Lightweight prompts (function / feature scope) ───────────
LIGHTWEIGHT_PLAN_SYSTEM_PROMPT: str = _p["lightweight_plan"]["system"]
LIGHTWEIGHT_GENERATE_SYSTEM_PROMPT: str = _p["lightweight_generate"]["system"]

# ── Full-stack phase prompts ─────────────────────────────────
FUNCTIONAL_DESIGN_SYSTEM_PROMPT: str = _p["functional_design"]["system"]
PLAN_SYSTEM_PROMPT: str = _p["plan"]["system"]
GENERATE_SYSTEM_PROMPT: str = _p["generate"]["system"]
VALIDATE_SYSTEM_PROMPT: str = _p["validate"]["system"]

# ── File-specific hints ──────────────────────────────────────
FILE_CONTEXT: dict[str, str] = _p["file_context"]
ROUTER_HINT: str = _p["router_hint"]
SERVICE_HINT: str = _p["service_hint"]
COMPONENT_HINT: str = _p["component_hint"]
