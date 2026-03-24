"""Subprocess helpers for runtime QA: environment setup, test execution, and compile checks.

Covers both Python (venv / pytest / py_compile) and JavaScript (npm / jest/vitest / node --check / tsc).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

from graph.state import CodeFile


# ---------------------------------------------------------------------------
# Python helpers
# ---------------------------------------------------------------------------


def find_requirements(
    code_files: list[CodeFile], project_dir: Path
) -> Path | None:
    """Return the path to the best requirements.txt found in the project.

    Args:
        code_files: Generated source files to search for a requirements entry.
        project_dir: Root directory of the generated project on disk.

    Returns:
        Absolute path to requirements.txt, or ``None`` if not found.
    """
    candidates = ["requirements.txt", "backend/requirements.txt"]
    for candidate in candidates:
        for cf in code_files:
            if cf.path == candidate:
                return project_dir / candidate
    for candidate in candidates:
        p = project_dir / candidate
        if p.exists():
            return p
    return None


def setup_environment(
    venv_dir: Path, project_dir: Path, code_files: list[CodeFile]
) -> tuple[bool, str]:
    """Create venv (if absent) and pip install project requirements.

    Args:
        venv_dir: Where to create the virtual environment.
        project_dir: Root of the generated project on disk.
        code_files: Generated code files (used to locate requirements.txt).

    Returns:
        ``(success, error_message)``
    """
    python_bin = venv_dir / "bin" / "python"
    if not python_bin.exists():
        print(f"[SETUP] Creating venv at {venv_dir} …")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return False, f"venv creation failed:\n{result.stderr}"
    else:
        print("[SETUP] Venv already exists — reusing.")

    req_path = find_requirements(code_files, project_dir)
    if req_path and req_path.exists():
        print(f"[SETUP] Installing {req_path.relative_to(project_dir)} …")
        pip = venv_dir / "bin" / "pip"
        result = subprocess.run(
            [str(pip), "install", "-r", str(req_path), "-q"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return False, f"pip install failed:\n{result.stderr}"
    else:
        print("[SETUP] No requirements.txt found — skipping pip install.")

    return True, ""


def extract_failed_summaries(output: str) -> list[str]:
    """Extract one summary string per failing test from pytest ``--tb=short`` output.

    Args:
        output: Combined stdout + stderr from a pytest run.

    Returns:
        List of failure summary strings. Falls back to the full output as one
        entry when no ``FAILED``/``ERROR`` lines are detected.
    """
    summaries: list[str] = []
    lines = output.splitlines()
    current: list[str] = []
    for line in lines:
        if re.match(r"^FAILED\s+", line) or re.match(r"^ERROR\s+", line):
            if current:
                summaries.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        summaries.append("\n".join(current))
    if not summaries and output.strip():
        summaries = [output.strip()]
    return summaries


def run_pytest(
    venv_dir: Path, project_dir: Path
) -> tuple[bool, str, list[str]]:
    """Run pytest against the project directory.

    Args:
        venv_dir: Virtual environment containing pytest.
        project_dir: Root of the generated project.

    Returns:
        ``(passed, full_output, failed_summaries)``
    """
    pytest_bin = venv_dir / "bin" / "pytest"
    if not pytest_bin.exists():
        pip = venv_dir / "bin" / "pip"
        subprocess.run(
            [str(pip), "install", "pytest", "-q"],
            capture_output=True,
            timeout=60,
        )

    print("[TEST] Running pytest …")
    result = subprocess.run(
        [str(pytest_bin), str(project_dir), "--tb=short", "-q", "--no-header"],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        timeout=180,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0
    failed_summaries = extract_failed_summaries(output) if not passed else []

    status = (
        "✅ PASS" if passed else f"❌ FAIL ({len(failed_summaries)} failure(s))"
    )
    print(f"[TEST] {status}")
    return passed, output, failed_summaries


def detect_server_entry(
    code_files: list[CodeFile],
) -> tuple[str, str] | tuple[None, None]:
    """Detect if the project has a runnable server entry point.

    Args:
        code_files: Generated source files to scan.

    Returns:
        ``(module_dotted_path, app_variable)`` or ``(None, None)`` if none found.
    """
    server_indicators = [
        ("FastAPI()", "app"),
        ("flask.Flask(__name__)", "app"),
        ("Flask(__name__)", "app"),
    ]
    for cf in code_files:
        if cf.language != "python":
            continue
        for indicator, var in server_indicators:
            if indicator in cf.content:
                module = (
                    cf.path.removesuffix(".py")
                    .replace("/", ".")
                    .replace("\\", ".")
                )
                return module, var
    return None, None


def check_server_health(
    python: Path, project_dir: Path, module: str, app_var: str
) -> tuple[bool, str]:
    """Start the server, poll for a healthy response, then terminate it.

    Args:
        python: Path to the venv python binary.
        project_dir: Root directory for subprocess cwd.
        module: Dotted module path (e.g. ``'backend.main'``).
        app_var: ASGI app variable name (e.g. ``'app'``).

    Returns:
        ``(healthy, error_message)``
    """
    port = 18999
    proc = None
    try:
        proc = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                f"{module}:{app_var}",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            cwd=str(project_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        urls = [
            f"http://127.0.0.1:{port}/health",
            f"http://127.0.0.1:{port}/",
        ]
        for _ in range(20):
            time.sleep(0.5)
            if proc.poll() is not None:
                stderr = (
                    proc.stderr.read().decode(errors="replace")
                    if proc.stderr
                    else ""
                )
                return False, f"Server exited early:\n{stderr}"
            for url in urls:
                try:
                    urllib.request.urlopen(url, timeout=1)
                    return True, ""
                except Exception:
                    pass
        return False, f"Server did not respond within 10s on port {port}"
    except Exception as exc:
        return False, str(exc)
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def compile_project(
    venv_dir: Path, project_dir: Path, code_files: list[CodeFile]
) -> tuple[bool, str]:
    """Compile all Python files via py_compile, then optionally run a server health check.

    Args:
        venv_dir: Virtual environment directory.
        project_dir: Root of the generated project.
        code_files: Generated source files.

    Returns:
        ``(passed, error_output)``
    """
    python = venv_dir / "bin" / "python"
    errors: list[str] = []

    print("[COMPILE] Running py_compile on all Python files …")
    for cf in code_files:
        if cf.language != "python":
            continue
        file_path = project_dir / cf.path
        if not file_path.exists():
            continue
        result = subprocess.run(
            [str(python), "-m", "py_compile", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            errors.append(f"{cf.path}: {result.stderr.strip()}")

    if errors:
        print(f"[COMPILE] ❌ {len(errors)} compile error(s)")
        return False, "\n".join(errors)

    print("[COMPILE] ✅ All Python files compile cleanly")

    module, app_var = detect_server_entry(code_files)
    if module:
        print(
            f"[COMPILE] Server detected ({module}:{app_var}) — running health check …"
        )
        ok, err = check_server_health(python, project_dir, module, app_var)
        if not ok:
            print("[COMPILE] ❌ Server health check failed")
            return False, err
        print("[COMPILE] ✅ Server started and responded")

    return True, ""


# ---------------------------------------------------------------------------
# JavaScript / TypeScript helpers
# ---------------------------------------------------------------------------


def setup_js_environment(project_dir: Path) -> tuple[bool, str]:
    """Run ``npm install`` if ``package.json`` exists in the project directory.

    Args:
        project_dir: Root of the generated project on disk.

    Returns:
        ``(success, error_message)``
    """
    pkg = project_dir / "package.json"
    if not pkg.exists():
        print("[SETUP] No package.json found — skipping npm install.")
        return True, ""

    print("[SETUP] Running npm install …")
    result = subprocess.run(
        ["npm", "install"],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        timeout=300,
    )
    if result.returncode != 0:
        return False, f"npm install failed:\n{result.stderr}"
    print("[SETUP] npm install completed.")
    return True, ""


def extract_js_failed_summaries(output: str) -> list[str]:
    """Extract one summary string per failing test from jest/vitest/mocha output.

    Args:
        output: Combined stdout + stderr from the test runner.

    Returns:
        List of failure summary strings, or the full output as a single entry
        when no structured failure markers are found.
    """
    summaries: list[str] = []
    lines = output.splitlines()
    current: list[str] = []

    for line in lines:
        # Jest:   "  * Suite > test name"  (bullet + suite separator)
        # Vitest: "  x test name" or "FAIL src/foo.test.js"
        # Mocha:  line starting with a number then "failing"
        if (
            re.match(r"^\s+\u25cf\s+", line)
            or re.match(r"^\s+\xd7\s+", line)
            or re.match(r"^FAIL\s+", line)
        ):
            if current:
                summaries.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)

    if current:
        summaries.append("\n".join(current))
    if not summaries and output.strip():
        summaries = [output.strip()]
    return summaries


def run_js_tests(
    project_dir: Path,
) -> tuple[bool, str, list[str]]:
    """Run ``npm test`` if a ``test`` script is defined in ``package.json``.

    Args:
        project_dir: Root of the generated project on disk.

    Returns:
        ``(passed, full_output, failed_summaries)``
    """
    pkg = project_dir / "package.json"
    if not pkg.exists():
        print("[TEST] No package.json — skipping JS tests.")
        return True, "", []

    try:
        manifest = json.loads(pkg.read_text(encoding="utf-8"))
    except Exception:
        print("[TEST] Could not parse package.json — skipping JS tests.")
        return True, "", []

    if "test" not in manifest.get("scripts", {}):
        print("[TEST] No test script in package.json — skipping JS tests.")
        return True, "", []

    print("[TEST] Running npm test …")
    result = subprocess.run(
        ["npm", "test", "--", "--no-coverage"],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        timeout=180,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0
    failed_summaries = extract_js_failed_summaries(output) if not passed else []

    status = (
        "✅ PASS" if passed else f"❌ FAIL ({len(failed_summaries)} failure(s))"
    )
    print(f"[TEST] {status}")
    return passed, output, failed_summaries


def check_js_syntax(
    project_dir: Path, code_files: list[CodeFile]
) -> tuple[bool, str]:
    """Check JS/TS syntax: ``tsc --noEmit`` for TypeScript, ``node --check`` for JS.

    Args:
        project_dir: Root of the generated project on disk.
        code_files: Generated source files to check.

    Returns:
        ``(passed, error_output)``
    """
    errors: list[str] = []
    has_ts = any(Path(cf.path).suffix in {".ts", ".tsx"} for cf in code_files)

    if has_ts:
        print("[COMPILE] Running tsc --noEmit …")
        result = subprocess.run(
            ["npx", "--yes", "tsc", "--noEmit"],
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=120,
        )
        if result.returncode != 0:
            errors.append((result.stdout + result.stderr).strip())
    else:
        print("[COMPILE] Running node --check on JS files …")
        for cf in code_files:
            if Path(cf.path).suffix not in {".js", ".mjs", ".cjs"}:
                continue
            file_path = project_dir / cf.path
            if not file_path.exists():
                continue
            result = subprocess.run(
                ["node", "--check", str(file_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                errors.append(f"{cf.path}: {result.stderr.strip()}")

    if errors:
        print(f"[COMPILE] ❌ {len(errors)} syntax error(s)")
        return False, "\n".join(errors)

    print("[COMPILE] ✅ JS/TS syntax checks passed")
    return True, ""
