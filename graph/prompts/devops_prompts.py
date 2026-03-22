"""DevOps agent prompts — loaded from delivery_engineer.yaml."""

from graph.prompts import load_prompts as _load


_p = _load("delivery_engineer")
_hint = _p["file_hint"]

DECISION_PROMPT: str = _p["decision"]["classify"]
CI_SYSTEM_PROMPT: str = _p["generate"]["ci_system"]
CD_SYSTEM_PROMPT: str = _p["generate"]["cd_system"]

# Inject pinned GitHub Actions SHAs into the ci_workflow hint
_sha = {
    "checkout": _p["actions"]["checkout"],
    "setup_python": _p["actions"]["setup_python"],
    "cache": _p["actions"]["cache"],
}
_hint["ci_workflow"] = _hint["ci_workflow"].format(**_sha)

FILE_HINT: dict[str, str] = _hint
