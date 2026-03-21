"""Prompt loading and management for agents."""

from pathlib import Path
import yaml


class PromptManager:
    """Load and manage prompts from YAML files."""

    # Prompt files that must all be present for a directory to be considered complete.
    _REQUIRED = {"solution_architect", "software_engineer", "quality_engineer"}

    def __init__(self, prompts_root: Path = None):
        if prompts_root is None:
            # Walk up from this file's directory looking for a prompts/ folder
            # that contains ALL required prompt files.  This ensures we skip
            # backend/prompts/ (which only has quality_engineer.yaml) and find
            # the root-level prompts/ directory that has the full set.
            current = Path(__file__).resolve().parent
            best: Path | None = None  # best partial match (fallback)

            for _ in range(12):
                candidate = current / "prompts"
                if candidate.exists():
                    stems = {p.stem for p in candidate.glob("*.yaml")}
                    if self._REQUIRED.issubset(stems):
                        prompts_root = candidate
                        break
                    if best is None:
                        best = candidate  # first partial match for fallback
                current = current.parent

            if prompts_root is None:
                # Fall back to the first partial match or a hardcoded path.
                prompts_root = best or Path(__file__).resolve().parent / "prompts"

        self.root = prompts_root
        self.prompts = {}
        self._load_all()
        print(f"[INFO] Loaded prompts from: {self.root}")

    def _load_all(self) -> None:
        """Load all prompt files."""
        if not self.root.exists():
            print(f"[WARNING] Prompts root not found: {self.root}")
            return

        for yaml_file in self.root.glob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    self.prompts[yaml_file.stem] = yaml.safe_load(f)
                    print(f"[INFO] Loaded prompt: {yaml_file.stem}")
            except Exception as e:
                print(f"[WARNING] Failed to load {yaml_file}: {e}")

    def get_prompt(self, agent: str, section: str = None, key: str = None) -> str:
        """Get a prompt by agent name and optional section/key path."""
        if agent not in self.prompts:
            return ""

        data = self.prompts[agent]

        if section:
            if isinstance(data, dict) and section in data:
                data = data[section]
            else:
                return ""

        if key:
            if isinstance(data, dict) and key in data:
                data = data[key]
            else:
                return ""

        if isinstance(data, str):
            return data
        elif isinstance(data, dict):
            # If dict, try to find the first string value
            for v in data.values():
                if isinstance(v, str):
                    return v
        return str(data) if data else ""
