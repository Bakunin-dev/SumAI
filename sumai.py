#!/usr/bin/env python3
"""
sumai.py

One-file, zero-dependency project summarizer.

Pipeline:
1) Scan the current project and write CodebaseDump.md
2) Build a compact repository context
3) Optionally call an LLM and write ReadmeDev.md

The file is intentionally structured as small testable nodes.
Each stage has a dedicated function and the pipeline returns per-stage timings.

How to use:
- put this file anywhere (or keep it outside the project)
- edit the AI CONFIG block near the top
- run: python sumai.py all                        # write CodebaseDump.md + ReadmeDev.md
- run: python sumai.py dump                       # write CodebaseDump.md only (no AI)
- run: python sumai.py readme                     # write ReadmeDev.md only (no dump saved)
- run: python sumai.py [command] --root /path     # scan a specific directory
"""

from __future__ import annotations

import ast
import datetime as _dt
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import tempfile
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Literal, cast

# ============================================================================
# AI CONFIG
# Keep this block tiny.
# Most users should only change AI_MODEL_PRESET.
# ============================================================================

AI_ENABLED = True

# Preferred path: choose one preset from MODEL_REGISTRY below.
AI_MODEL_PRESET = "mistral_small"

# Optional manual overrides.
# Leave empty to use the preset defaults.
AI_PROVIDER_NAME_OVERRIDE = ""
AI_PROTOCOL_OVERRIDE = ""
AI_BASE_URL_OVERRIDE = ""
AI_MODEL_OVERRIDE = ""
AI_API_KEY_OVERRIDE = ""

AI_REQUEST_TIMEOUT_SECONDS = 300
AI_REQUEST_GAP_SECONDS = 2.0
AI_REQUIRE_FULL_CONTEXT = False
AI_MAX_CONTEXT_CHARS = 600_000
AI_MAX_SELECTED_FILES = 180
AI_MAX_OUTPUT_TOKENS = 8000
AI_TEMPERATURE = 0.7


AIProtocol = Literal["chat_completions", "responses"]


@dataclass(frozen=True)
class ModelSpec:
    provider_name: str
    protocol: AIProtocol
    base_url: str
    model: str
    env_keys: tuple[str, ...] = ()


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "mistral_small": ModelSpec(
        provider_name="mistral_small",
        protocol="chat_completions",
        base_url="https://api.mistral.ai/v1",
        model="mistral-small-2603",
        env_keys=("MISTRAL_API_KEY", "AI_API_KEY"),
    ),
    "glm_flash": ModelSpec(
        provider_name="zai_glm_flash",
        protocol="chat_completions",
        base_url="https://api.z.ai/api/paas/v4",
        model="glm-4.7-flash",
        env_keys=("ZAI_API_KEY", "AI_API_KEY"),
    ),
    "openai_gpt5": ModelSpec(
        provider_name="openai_gpt5",
        protocol="responses",
        base_url="https://api.openai.com/v1",
        model="gpt-5",
        env_keys=("OPENAI_API_KEY", "AI_API_KEY"),
    ),
}


def first_non_empty(*values: str) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def first_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def resolve_ai_settings(
    preset: str,
    *,
    provider_name_override: str = "",
    protocol_override: str = "",
    base_url_override: str = "",
    model_override: str = "",
    api_key_override: str = "",
) -> tuple[str, AIProtocol, str, str, str]:
    spec = MODEL_REGISTRY.get(preset)
    if spec is None:
        known = ", ".join(sorted(MODEL_REGISTRY))
        raise ValueError(f"Unknown AI_MODEL_PRESET: {preset!r}. Known presets: {known}")

    raw_protocol = first_non_empty(protocol_override, spec.protocol)
    if raw_protocol not in {"chat_completions", "responses"}:
        raise ValueError(f"Unsupported ai_protocol: {raw_protocol}")
    protocol = cast(AIProtocol, raw_protocol)

    provider_name = first_non_empty(provider_name_override, spec.provider_name)
    base_url = first_non_empty(base_url_override, spec.base_url)
    model = first_non_empty(model_override, spec.model)
    api_key = first_non_empty(
        api_key_override,
        first_env_value(spec.env_keys),
        os.environ.get("AI_API_KEY", ""),
        "PASTE_YOUR_API_KEY_HERE",
    )

    return provider_name, protocol, base_url, model, api_key


AI_PROVIDER_NAME, AI_PROTOCOL, AI_BASE_URL, AI_MODEL, AI_API_KEY = resolve_ai_settings(
    AI_MODEL_PRESET,
    provider_name_override=AI_PROVIDER_NAME_OVERRIDE,
    protocol_override=AI_PROTOCOL_OVERRIDE,
    base_url_override=AI_BASE_URL_OVERRIDE,
    model_override=AI_MODEL_OVERRIDE,
    api_key_override=AI_API_KEY_OVERRIDE,
)

# ============================================================================
# AI SYSTEM PROMPT
# ============================================================================

AI_SYSTEM_PROMPT = (
    "Use only the repository context you are given. Treat runtime code, configuration, tests, scripts, and dependency files as stronger evidence than markdown docs. "
    "Prefer practical navigation, operational usefulness, and technical clarity over generic summaries. "
    "If something is missing or unclear, say 'Not found in provided context' instead of inventing details."
)

# ============================================================================
# ARTIFACT & PROMPT CONFIG
# Edit prompts here to customize AI behavior.
# ============================================================================

ARTIFACT_FOCUS = (
    'Collect a broad, dense, high-signal technical research artifact for a later README_DEV pass. '
    'Prioritize repository structure, key files, entrypoints, runtime flow, architecture, data/domain model, '
    'commands, verification, config/environment, docs/scripts/ops signals, extension points, risks, gaps, and unknowns. '
    'The goal is not a minimal summary. The goal is a rich technical handoff that preserves as much useful grounded context as possible.'
)

RESEARCH_PROMPT_SECTIONS = [
    "## Scope",
    "## Project Identity",
    "## Project Skeleton",
    "## Directory And File Guide",
    "## Tech Stack And Tooling",
    "## Entrypoints And Runtime",
    "## Architecture Overview",
    "## Core Data And Domain",
    "## Commands And Verification",
    "## Configuration And Environment",
    "## Docs Scripts Ops And Integrations",
    "## Extension Points",
    "## Risks Gaps And Unknowns",
    "## Evidence Index",
    "## Aggregator Carry-Forward",
]

RESEARCH_PROMPT_GOAL = (
    "Collect a rich, grounded technical research pack for a later README_DEV pass. "
    "Prioritize structure, key files, entrypoints, runtime flow, architecture, data/domain model, "
    "commands, verification, config, ops signals, extension points, risks, gaps, and unknowns."
)

RESEARCH_PROMPT_RULES = [
    "Use only the provided repository context.",
    "Prefer runtime code, config, tests, scripts, dependency files, and CI/deploy files over markdown docs.",
    "Preserve useful technical detail; do not optimize for shortness.",
    "Use exact file paths, modules, classes, functions, env vars, commands, endpoints, and symbols whenever possible.",
    "Show a compact project skeleton with only important folders and key files.",
    "Summarize only important directories and files, and explain why they matter.",
    "Include only explicit or strongly evidenced commands.",
    "Mark non-explicit architecture or structure as `Likely`.",
    "Describe main entities, states, stores, and relationships when present.",
    "Explain where new features, handlers, services, modules, entities, or tests are usually added.",
    "Separate confirmed gaps, likely weak spots, and unknown areas.",
    "Use `[Confirmed]`, `[Likely]`, and `[Unknown]`.",
    "Support non-trivial claims with file paths inline or nearby.",
    "Do not invent commands, services, deployment targets, databases, external systems, architecture, or workflows.",
    "Do not write the final README_DEV.",
    "In Evidence Index, list the strongest supporting files and why they matter.",
    "In Aggregator Carry-Forward, provide a dense bullet list of the most important grounded facts, commands, invariants, boundaries, and structural cues.",
]

README_PROMPT_SECTIONS = [
    "# <Project Name> — README_DEV",
    "Short opening paragraph",
    "## What This Project Is",
    "## Project Skeleton",
    "## Directory And File Guide",
    "## Technical Summaries",
    "### Stack Summary",
    "### Tooling Summary",
    "### Configuration Summary",
    "### Testing Summary",
    "### Operations Summary",
    "## Entry Points And Runtime Flow",
    "## Architecture Overview",
    "### Layers And Responsibilities",
    "### Key Module Relationships",
    "### Main Execution Paths",
    "## Core Data And Domain Model",
    "## Key Commands",
    "## Verification And Done Criteria",
    "## Architectural Invariants",
    "## Safety And Boundaries",
    "## Configuration And Environment",
    "## Extension Guide",
    "## Known Gaps And Technical Debt",
    "## Useful Pointers",
    "## Analysis Notes",
]

README_PROMPT_RULES = [
    "Use the research artifact to focus attention, then verify everything against repository context.",
    "If artifact and repository context conflict, prefer repository context.",
    "Prefer runtime code, config, tests, scripts, dependency files, CI/deploy files, and executable evidence over markdown docs.",
    "Produce a rich, grounded README_DEV, not a minimal summary.",
    "Include a compact but useful skeleton tree with only important folders and key files.",
    "Summarize only important areas; do not explain every file.",
    "Include short technical summaries with real value, not filler.",
    "Use exact file paths, modules, symbols, commands, env vars, and script names whenever possible.",
    "Include practical guidance on where changes are usually made and what to verify after changes.",
    "Avoid generic advice that would fit any repository.",
    "Do not paraphrase code unless it adds navigational or operational value.",
    "Do not invent commands, deployment targets, services, data stores, architecture, CI, security controls, or workflows.",
    "Label doc-derived points as `Doc-stated`.",
    "Label plausible but non-explicit points as `Likely`.",
    "For missing information, say `Not found in provided context`.",
    "Do not treat generated artifacts or temp files as core project docs unless clearly part of the workflow.",
    "Keep the tone dry, technical, and useful.",
]

# ============================================================================
# OUTPUT / PROJECT CONFIG
# ============================================================================

OUTPUT_DUMP_NAME = "CodebaseDump.md"
OUTPUT_README_NAME = "ReadmeDev.md"
OUTPUT_ARTIFACT_DIR_NAME = "sumai_artifacts"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
SCRIPT_NAME = pathlib.Path(__file__).name
PREFER_GIT_FILE_DISCOVERY = True
VERBOSE_EXPLAIN = False

# Safety / scale limits
MAX_FILE_BYTES = 300_000
MAX_TREE_FILES = 12_000
BINARY_SNIFF_BYTES = 8_192

# ============================================================================
# FILTER POLICY
# ============================================================================

EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".jj",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".svelte-kit",
    ".turbo",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "dist",
    "build",
    "target",
    "coverage",
    ".coverage_html",
    ".gradle",
    ".terraform",
    ".serverless",
    ".aws-sam",
    ".dart_tool",
    ".yarn",
    ".pnpm-store",
    ".gitlab",
    OUTPUT_ARTIFACT_DIR_NAME,
}

EXCLUDED_FILE_NAMES = {
    OUTPUT_DUMP_NAME,
    OUTPUT_README_NAME,
    SCRIPT_NAME,
    "AIContext.md",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".coverage",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_dsa",
}

EXCLUDED_SUFFIXES = {
    ".pyc", ".pyo", ".pyd",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico", ".svg",
    ".mp3", ".wav", ".ogg", ".flac", ".aac",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".jar", ".war", ".so", ".dylib", ".dll", ".exe", ".bin", ".obj", ".o", ".a", ".lib",
    ".class", ".sqlite", ".db", ".sqlite3",
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
    ".min.js", ".min.css",
}

EXCLUDED_GLOBS = [
    "*.log",
    "*.tmp",
    "*.swp",
    "*.swo",
    "*.seed",
    ".env",
    ".env.*",
    "*.env",
    "*.local",
    "secrets.*",
    "secret.*",
    "*.secret",
    "*.cache",
    "*.lock",
]

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".json": "json",
    ".jsonl": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".md": "markdown",
    ".tf": "hcl",
    ".ini": "ini",
    ".cfg": "ini",
    ".txt": "text",
    ".csv": "csv",
}

SPECIAL_FILENAMES = {
    "Dockerfile": "dockerfile",
    "docker-compose.yml": "yaml",
    "docker-compose.yaml": "yaml",
    "Makefile": "makefile",
    "CMakeLists.txt": "cmake",
    ".gitignore": "gitignore",
    ".gitattributes": "gitattributes",
    ".editorconfig": "ini",
    "requirements.txt": "text",
}

SENSITIVE_QUOTED_ASSIGNMENT_RE = re.compile(
    r'''(?im)^(\s*(?:export\s+)?["']?[\w.\-]*(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|database[_-]?url|db[_-]?url|connection[_-]?string|dsn|client[_-]?secret|private[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|authorization)[\w.\-]*["']?\s*[:=]\s*["'])([^"\r\n]+)(["'])'''
)

SENSITIVE_UNQUOTED_ASSIGNMENT_RE = re.compile(
    r'''(?im)^(\s*(?:export\s+)?["']?[\w.\-]*(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|database[_-]?url|db[_-]?url|connection[_-]?string|dsn|client[_-]?secret|private[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|authorization)[\w.\-]*["']?\s*[:=]\s*)([^\s#\r\n]+)'''
)

INLINE_SECRET_PATTERNS = [
    re.compile(r'(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)([A-Za-z0-9._~+/=\-]+)'),
    re.compile(r'([A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)'),
    re.compile(r'(AKIA[0-9A-Z]{16})'),
    re.compile(r'(sk-[A-Za-z0-9_\-]{12,})'),
    re.compile(r'(gh[pousr]_[A-Za-z0-9]{20,})'),
    re.compile(r'(glpat-[A-Za-z0-9\-_]{20,})'),
    re.compile(r'(xox[baprs]-[A-Za-z0-9-]{10,})'),
    re.compile(r'(AIza[0-9A-Za-z\-_]{20,})'),
    re.compile(r'(ya29\.[0-9A-Za-z\-_]+)'),
]

PRIVATE_KEY_BLOCK_RE = re.compile(
    r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----',
    re.DOTALL,
)

IMPORTANCE_PATTERNS = [
    (r"(^|/)README(\.[^/]+)?$", 120),
    (r"(^|/)pyproject\.toml$", 110),
    (r"(^|/)package\.json$", 110),
    (r"(^|/)requirements(\.[^/]+)?\.txt$", 105),
    (r"(^|/)setup\.py$", 102),
    (r"(^|/)pytest\.ini$", 98),
    (r"(^|/)Pipfile$", 100),
    (r"(^|/)Dockerfile$", 100),
    (r"(^|/)docker-compose(\.ya?ml)?$", 100),
    (r"(^|/)Makefile$", 95),
    (r"(^|/)compose\.ya?ml$", 95),
    (r"(^|/)main\.[A-Za-z0-9]+$", 90),
    (r"(^|/)app\.[A-Za-z0-9]+$", 85),
    (r"(^|/)server\.[A-Za-z0-9]+$", 85),
    (r"(^|/)manage\.py$", 85),
    (r"(^|/)settings\.[A-Za-z0-9]+$", 80),
    (r"(^|/)routes?\.[A-Za-z0-9]+$", 75),
    (r"(^|/)config\.[A-Za-z0-9]+$", 75),
    (r"(^|/)src/", 25),
    (r"(^|/)app/", 20),
    (r"(^|/)core/", 20),
    (r"(^|/)tests?/", -30),
    (r"(^|/)migrations?/", -25),
]

NON_TEXT_BYTES = set(range(0, 9)) | {11, 12} | set(range(14, 32))

# ============================================================================
# DATA MODELS
# ============================================================================


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: pathlib.Path
    dump_name: str = OUTPUT_DUMP_NAME
    readme_name: str = OUTPUT_README_NAME
    artifact_dir_name: str = OUTPUT_ARTIFACT_DIR_NAME
    script_name: str = SCRIPT_NAME
    prefer_git_file_discovery: bool = PREFER_GIT_FILE_DISCOVERY
    max_file_bytes: int = MAX_FILE_BYTES
    max_tree_files: int = MAX_TREE_FILES
    binary_sniff_bytes: int = BINARY_SNIFF_BYTES
    ai_enabled: bool = AI_ENABLED
    verbose_explain: bool = VERBOSE_EXPLAIN
    ai_provider_name: str = AI_PROVIDER_NAME
    ai_protocol: AIProtocol = AI_PROTOCOL
    ai_base_url: str = AI_BASE_URL
    ai_model: str = AI_MODEL
    ai_api_key: str = AI_API_KEY
    ai_request_timeout_seconds: int = AI_REQUEST_TIMEOUT_SECONDS
    ai_request_gap_seconds: float = AI_REQUEST_GAP_SECONDS
    ai_require_full_context: bool = AI_REQUIRE_FULL_CONTEXT
    ai_max_context_chars: int = AI_MAX_CONTEXT_CHARS
    ai_max_selected_files: int = AI_MAX_SELECTED_FILES
    ai_max_output_tokens: int = AI_MAX_OUTPUT_TOKENS
    ai_temperature: float | None = AI_TEMPERATURE
    ai_system_prompt: str = AI_SYSTEM_PROMPT
    write_dump: bool = True
    write_readme: bool = True
    simple_ignore_patterns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def dump_path(self) -> pathlib.Path:
        return self.project_root / self.dump_name

    @property
    def readme_path(self) -> pathlib.Path:
        return self.project_root / self.readme_name

    @property
    def artifact_dir(self) -> pathlib.Path:
        return self.project_root / self.artifact_dir_name


@dataclass(frozen=True)
class DiscoveryResult:
    files: list[pathlib.Path]
    backend: str
    truncated: bool


@dataclass(frozen=True)
class FileStats:
    included: int = 0
    skipped_binary: int = 0
    skipped_large: int = 0
    skipped_unreadable: int = 0


@dataclass(frozen=True)
class FileRecord:
    rel_path: str
    abs_path: str
    size: int
    mtime_ns: int
    language: str
    importance: int
    is_binary: bool
    is_large: bool
    is_unreadable: bool
    redacted_text: str | None


@dataclass(frozen=True)
class InspectionResult:
    records: list[FileRecord]
    stats: FileStats


@dataclass(frozen=True)
class DumpResult:
    text: str
    stats: FileStats


@dataclass(frozen=True)
class AIContextResult:
    text: str
    used_compact_mode: bool
    selected_files: int


@dataclass(frozen=True)
class ArtifactSpec:
    slug: str
    title: str
    output_name: str
    focus: str


@dataclass(frozen=True)
class ArtifactResult:
    slug: str
    title: str
    output_name: str
    prompt: str
    text: str


@dataclass(frozen=True)
class AIRequest:
    prompt: str
    payload: dict[str, Any]
    endpoint_url: str


@dataclass(frozen=True)
class AIResponse:
    text: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class StageTiming:
    duration_ms: float
    status: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineResult:
    return_code: int
    timings: dict[str, StageTiming]
    discovery: DiscoveryResult | None = None
    inspection: InspectionResult | None = None
    dump: DumpResult | None = None
    ai_context: AIContextResult | None = None
    ai_request: AIRequest | None = None
    ai_response: AIResponse | None = None
    artifacts: tuple[ArtifactResult, ...] = field(default_factory=tuple)


# ============================================================================
# SMALL HELPERS
# ============================================================================


def log(message: str) -> None:
    print(message, flush=True)


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass


def atomic_write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def posix_rel(config: RuntimeConfig, path: pathlib.Path) -> str:
    return path.relative_to(config.project_root).as_posix()


def file_language(path: pathlib.Path) -> str:
    if path.name in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[path.name]
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def load_simple_ignore_patterns(project_root: pathlib.Path) -> tuple[str, ...]:
    patterns: list[str] = []
    for name in (".sumaiignore", ".ignore", ".gitignore"):
        ignore_path = project_root / name
        if not ignore_path.exists() or not ignore_path.is_file():
            continue
        try:
            text = ignore_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            normalized = line.lstrip("/")
            if normalized.endswith("/"):
                patterns.append(normalized + "**")
            patterns.append(normalized)
    return tuple(patterns)


def build_runtime_config(project_root: pathlib.Path = PROJECT_ROOT) -> RuntimeConfig:
    root = pathlib.Path(project_root).resolve()
    return RuntimeConfig(project_root=root, simple_ignore_patterns=load_simple_ignore_patterns(root))


def matches_any_glob(rel_path: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    name = rel_path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def should_skip_path(config: RuntimeConfig, rel_path: str, is_dir: bool) -> bool:
    if not rel_path:
        return False

    name = rel_path.rsplit("/", 1)[-1]
    if is_dir and (name in EXCLUDED_DIR_NAMES or rel_path == config.artifact_dir_name):
        return True
    if not is_dir and name in EXCLUDED_FILE_NAMES:
        return True
    if matches_any_glob(rel_path, config.simple_ignore_patterns):
        return True

    if not is_dir:
        lower_name = name.lower()
        lower_rel = rel_path.lower()
        if any(lower_name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            return True
        if matches_any_glob(lower_name, tuple(EXCLUDED_GLOBS)) or matches_any_glob(lower_rel, tuple(EXCLUDED_GLOBS)):
            return True

    return False


# ============================================================================
# FILE DISCOVERY
# ============================================================================


def git_available(config: RuntimeConfig) -> bool:
    if not config.prefer_git_file_discovery:
        return False
    try:
        proc = subprocess.run(
            ["git", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(config.project_root),
            text=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def inside_git_repo(config: RuntimeConfig) -> bool:
    if not git_available(config):
        return False
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(config.project_root),
            text=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def collect_files_with_git(config: RuntimeConfig) -> DiscoveryResult:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(config.project_root),
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        error_text = proc.stderr.decode("utf-8", errors="replace").strip() or "git ls-files failed"
        raise RuntimeError(error_text)

    files: list[pathlib.Path] = []
    truncated = False
    for raw_item in (item for item in proc.stdout.split(b"\x00") if item):
        rel = raw_item.decode("utf-8", errors="replace").replace("\\", "/")
        if should_skip_path(config, rel, is_dir=False):
            continue
        path = config.project_root / rel
        if not path.exists() or not path.is_file() or path.is_symlink():
            continue
        files.append(path)
        if len(files) >= config.max_tree_files:
            truncated = True
            break

    files = sorted(files, key=lambda item: posix_rel(config, item))
    return DiscoveryResult(files=files, backend="git", truncated=truncated)


def walk_with_scandir(config: RuntimeConfig) -> DiscoveryResult:
    results: list[pathlib.Path] = []
    truncated = False

    def visit(directory: pathlib.Path) -> None:
        nonlocal truncated
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
        except OSError:
            return

        for entry in entries:
            if len(results) >= config.max_tree_files:
                truncated = True
                return

            entry_path = pathlib.Path(entry.path)
            try:
                rel = entry_path.relative_to(config.project_root).as_posix()
            except ValueError:
                continue

            if not rel or entry.is_symlink():
                continue

            if entry.is_dir(follow_symlinks=False):
                if should_skip_path(config, rel, is_dir=True):
                    continue
                visit(entry_path)
                continue

            if entry.is_file(follow_symlinks=False):
                if should_skip_path(config, rel, is_dir=False):
                    continue
                results.append(entry_path)

    visit(config.project_root)
    return DiscoveryResult(files=results, backend="filesystem", truncated=truncated)


def discover_project_files(config: RuntimeConfig, logger: Callable[[str], None] | None = None) -> DiscoveryResult:
    if inside_git_repo(config):
        try:
            result = collect_files_with_git(config)
            if result.files:
                return result
        except Exception as exc:
            if logger:
                logger(f"[sumai] Git discovery failed, falling back to filesystem scan: {exc}")
    return walk_with_scandir(config)


# ============================================================================
# FILE INSPECTION
# ============================================================================


def is_probably_binary_bytes(chunk: bytes) -> bool:
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    bad = sum(byte in NON_TEXT_BYTES for byte in chunk)
    return (bad / max(1, len(chunk))) > 0.30


def decode_text_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def redact_sensitive_text(text: str) -> str:
    redacted = PRIVATE_KEY_BLOCK_RE.sub("[REDACTED PRIVATE KEY BLOCK]", text)
    redacted = SENSITIVE_QUOTED_ASSIGNMENT_RE.sub(r"\1[REDACTED]\3", redacted)
    redacted = SENSITIVE_UNQUOTED_ASSIGNMENT_RE.sub(r"\1[REDACTED]", redacted)
    for pattern in INLINE_SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: (match.group(1) + "[REDACTED]" + match.group(3))
            if match.lastindex and match.lastindex >= 3
            else "[REDACTED]",
            redacted,
        )
    return redacted


def importance_score(rel_path: str) -> int:
    score = 0
    for pattern, delta in IMPORTANCE_PATTERNS:
        if re.search(pattern, rel_path):
            score += delta
    depth = rel_path.count("/")
    score -= depth * 2
    extension = pathlib.Path(rel_path).suffix.lower()
    if extension in {".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".toml", ".json", ".yaml", ".yml"}:
        score += 4
    return score


def inspect_single_file(config: RuntimeConfig, path: pathlib.Path) -> FileRecord:
    rel_path = posix_rel(config, path)
    stat = path.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns

    language = file_language(path)
    record = FileRecord(
        rel_path=rel_path,
        abs_path=str(path),
        size=size,
        mtime_ns=mtime_ns,
        language=language,
        importance=importance_score(rel_path),
        is_binary=False,
        is_large=False,
        is_unreadable=False,
        redacted_text=None,
    )

    if size > config.max_file_bytes:
        record = replace(record, is_large=True)
        return record

    try:
        raw = path.read_bytes()
    except OSError:
        record = replace(record, is_unreadable=True)
        return record

    if is_probably_binary_bytes(raw[: config.binary_sniff_bytes]):
        record = replace(record, is_binary=True)
        return record

    text = redact_sensitive_text(decode_text_bytes(raw))
    record = replace(record, redacted_text=text)
    return record


def inspect_project_files(config: RuntimeConfig, discovery: DiscoveryResult) -> InspectionResult:
    records: list[FileRecord] = []
    stats = FileStats()
    seen: set[str] = set()

    for path in discovery.files:
        rel_path = posix_rel(config, path)
        if rel_path in seen:
            continue
        seen.add(rel_path)

        try:
            stat = path.stat()
        except OSError:
            record = FileRecord(
                rel_path=rel_path,
                abs_path=str(path),
                size=0,
                mtime_ns=0,
                language=file_language(path),
                importance=importance_score(rel_path),
                is_binary=False,
                is_large=False,
                is_unreadable=True,
                redacted_text=None,
            )
            stats = replace(stats, skipped_unreadable=stats.skipped_unreadable + 1)
            records.append(record)
            continue

        record = inspect_single_file(config, path)

        if record.is_binary:
            stats = replace(stats, skipped_binary=stats.skipped_binary + 1)
        elif record.is_large:
            stats = replace(stats, skipped_large=stats.skipped_large + 1)
        elif record.is_unreadable:
            stats = replace(stats, skipped_unreadable=stats.skipped_unreadable + 1)
        else:
            stats = replace(stats, included=stats.included + 1)

        records.append(record)

    records.sort(key=lambda item: item.rel_path)
    return InspectionResult(records=records, stats=stats)


# ============================================================================
# REPOSITORY SHAPE + DUMP RENDERING
# ============================================================================


def build_tree(config: RuntimeConfig, paths: list[pathlib.Path]) -> str:
    tree: dict[str, dict[str, Any]] = {}
    for path in paths:
        node = tree
        for part in posix_rel(config, path).split("/"):
            node = node.setdefault(part, {})

    lines = [config.project_root.name + "/"]

    def render(node: dict[str, Any], prefix: str = "") -> None:
        names = sorted(node.keys())
        for index, name in enumerate(names):
            is_last = index == len(names) - 1
            connector = "└── " if is_last else "├── "
            lines.append(prefix + connector + name)
            extension = "    " if is_last else "│   "
            render(node[name], prefix + extension)

    render(tree)
    return "\n".join(lines)


def render_dump(config: RuntimeConfig, discovery: DiscoveryResult, inspection: InspectionResult) -> DumpResult:
    tree = build_tree(config, discovery.files)
    parts = [
        "# CodebaseDump",
        "",
        f"- Generated: {now_iso()}",
        f"- Project root: `{config.project_root}`",
        f"- Discovery backend: `{discovery.backend}`",
        f"- Files discovered: {len(discovery.files)}",
        f"- Included text files: {inspection.stats.included}",
        f"- Skipped binary: {inspection.stats.skipped_binary}",
        f"- Skipped large: {inspection.stats.skipped_large}",
        f"- Skipped unreadable: {inspection.stats.skipped_unreadable}",
        "",
        "## Repository Tree",
        "",
        "```text",
        tree,
        "```",
        "",
        "## Files",
        "",
    ]

    for record in inspection.records:
        if record.is_binary:
            parts.extend([f"### {record.rel_path}", "", "Skipped: binary file.", ""])
            continue
        if record.is_large:
            parts.extend([f"### {record.rel_path}", "", f"Skipped: file is larger than {config.max_file_bytes} bytes.", ""])
            continue
        if record.is_unreadable:
            parts.extend([f"### {record.rel_path}", "", "Skipped: unreadable file.", ""])
            continue
        parts.extend([
            f"### {record.rel_path}",
            "",
            f"```{record.language}",
            record.redacted_text or "",
            "```",
            "",
        ])

    return DumpResult(text="\n".join(parts).rstrip() + "\n", stats=inspection.stats)


# ============================================================================
# AI CONTEXT + PROMPT
# ============================================================================


def normalize_ai_context_text(text: str) -> str:
    normalized_lines = []
    volatile_prefixes = (
        '- Generated: ',
        '- File cache hits: ',
        '- File cache misses: ',
    )
    for line in text.splitlines():
        if line.startswith(volatile_prefixes):
            continue
        normalized_lines.append(line)
    return "\n".join(normalized_lines).strip() + "\n"


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def is_doc_like_path(rel_path: str) -> bool:
    lower = rel_path.lower()
    name = lower.rsplit('/', 1)[-1]
    stem = pathlib.PurePosixPath(name).stem
    return (
        lower.endswith('.md')
        or lower.startswith('docs/')
        or '/docs/' in lower
        or stem in {'readme', 'changelog', 'license', 'contributing', 'authors', 'notes'}
    )


def is_benchmark_like_path(rel_path: str) -> bool:
    lower = rel_path.lower()
    name = lower.rsplit('/', 1)[-1]
    return 'benchmark' in lower or name in {'benchmark_results.json', 'profile.json'}


def is_test_like_path(rel_path: str) -> bool:
    lower = rel_path.lower()
    name = lower.rsplit('/', 1)[-1]
    return (
        lower.startswith('tests/')
        or '/tests/' in lower
        or name.startswith('test_')
        or name.endswith('_test.py')
        or name.endswith('.spec.ts')
        or name.endswith('.spec.js')
        or name.endswith('.test.ts')
        or name.endswith('.test.js')
    )


def is_config_like_path(rel_path: str, text: str) -> bool:
    lower = rel_path.lower()
    if (
        lower.startswith('config/')
        or '/config/' in lower
        or re.search(r'(^|/)(config|settings|env)(\.[^/]+)?$', lower)
        or lower.endswith('.env')
        or lower.endswith('.ini')
        or lower.endswith('.toml')
        or lower.endswith('.yaml')
        or lower.endswith('.yml')
        or lower.endswith('.json')
    ):
        return True
    constant_names = re.findall(r'(?m)^([A-Z][A-Z0-9_]{2,})\s*=', text)
    return len(constant_names) >= 3


def python_module_aliases(rel_path: str) -> list[str]:
    pure = pathlib.PurePosixPath(rel_path)
    if pure.suffix != '.py':
        return []

    parts = list(pure.parts)
    if not parts:
        return []

    if parts[-1] == '__init__.py':
        module_parts = parts[:-1]
    else:
        module_parts = parts[:-1] + [pure.stem]

    aliases: list[str] = []
    for start in range(len(module_parts)):
        alias = '.'.join(module_parts[start:])
        if alias:
            aliases.append(alias)
    if module_parts:
        aliases.append(module_parts[-1])
    return dedupe_preserve_order(aliases)


def call_name_from_ast(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.AST | None = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return '.'.join(reversed(parts))
    return None


def is_python_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left = test.left
    right = test.comparators[0]
    if not isinstance(left, ast.Name) or left.id != '__name__':
        return False
    if not isinstance(right, ast.Constant) or right.value != '__main__':
        return False
    return isinstance(test.ops[0], ast.Eq)


def extract_python_outline(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        'functions': [],
        'classes': [],
        'imports': [],
        'main_guard_calls': [],
        'main_function_calls': [],
        'constants': [],
        'dataclass_classes': [],
        'has_main_guard': False,
        'has_dispatcher_pattern': False,
    }
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return info

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info['functions'].append(node.name)
            if node.name == 'main':
                calls: list[str] = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        name = call_name_from_ast(child.func)
                        if name:
                            calls.append(name)
                info['main_function_calls'] = dedupe_preserve_order(calls)[:8]
        elif isinstance(node, ast.ClassDef):
            info['classes'].append(node.name)
            decorator_names = {call_name_from_ast(decorator) for decorator in node.decorator_list}
            if 'dataclass' in decorator_names:
                info['dataclass_classes'].append(node.name)
        elif isinstance(node, ast.Import):
            info['imports'].extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                info['imports'].append(node.module)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and re.match(r'^[A-Z][A-Z0-9_]{2,}$', target.id):
                    info['constants'].append(target.id)
        elif isinstance(node, ast.If) and is_python_main_guard(node.test):
            info['has_main_guard'] = True
            calls: list[str] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    name = call_name_from_ast(child.func)
                    if name:
                        calls.append(name)
            info['main_guard_calls'] = dedupe_preserve_order(calls)[:8]

    text_lower = text.lower()
    if (
        re.search(r'(?m)^\s*def\s+(handle_request|dispatch|route_command|run_command)\b', text)
        or ('command =' in text_lower and 'elif command ==' in text_lower)
        or ('args[0]' in text and 'return' in text_lower)
    ):
        info['has_dispatcher_pattern'] = True

    info['functions'] = dedupe_preserve_order(info['functions'])[:10]
    info['classes'] = dedupe_preserve_order(info['classes'])[:10]
    info['imports'] = dedupe_preserve_order(info['imports'])[:16]
    info['constants'] = dedupe_preserve_order(info['constants'])[:16]
    info['dataclass_classes'] = dedupe_preserve_order(info['dataclass_classes'])[:8]
    return info


def extract_js_ts_outline(text: str) -> tuple[list[str], list[str]]:
    exports = re.findall(r"(?m)^\s*export\s+(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    exports += re.findall(r"(?m)^\s*export\s+(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    classes = re.findall(r"(?m)^\s*export\s+class\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    return dedupe_preserve_order(exports)[:8], dedupe_preserve_order(classes)[:8]


def extract_env_var_names(text: str) -> list[str]:
    patterns = [
        r'os\.environ\.get\(["\']([A-Z][A-Z0-9_]{1,})["\']',
        r'os\.getenv\(["\']([A-Z][A-Z0-9_]{1,})["\']',
        r'getenv\(["\']([A-Z][A-Z0-9_]{1,})["\']',
        r'process\.env\.([A-Z][A-Z0-9_]{1,})',
        r'process\.env\[["\']([A-Z][A-Z0-9_]{1,})["\']\]',
        r'System\.getenv\(["\']([A-Z][A-Z0-9_]{1,})["\']',
    ]
    env_vars: list[str] = []
    for pattern in patterns:
        env_vars.extend(re.findall(pattern, text))
    return dedupe_preserve_order(env_vars)[:30]


def summarize_record_for_context(record: FileRecord) -> dict[str, Any]:
    rel_path = record.rel_path
    rel_lower = rel_path.lower()
    text = record.redacted_text or ''

    roles: list[str] = []
    reasons: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    imports: list[str] = []
    exports: list[str] = []
    main_calls: list[str] = []
    constants: list[str] = []
    dataclass_classes: list[str] = []

    is_doc = is_doc_like_path(rel_path)
    is_benchmark = is_benchmark_like_path(rel_path)
    is_test = is_test_like_path(rel_path)
    is_config = is_config_like_path(rel_path, text)

    if is_doc:
        roles.append('doc')
        reasons.append('documentation file')
    if is_benchmark:
        roles.append('benchmark')
        reasons.append('benchmark or measurement file')
    if is_test:
        roles.append('test')
        reasons.append('test surface')
    if is_config:
        roles.append('config')
        reasons.append('config surface')

    if re.search(r'(^|/)(utils?|helpers?)\.[a-z0-9]+$', rel_lower):
        roles.append('utility')
        reasons.append('utility module')

    if record.language == 'python' and text:
        py_info = extract_python_outline(text)
        functions = py_info['functions']
        classes = py_info['classes']
        imports = py_info['imports']
        main_calls = dedupe_preserve_order(py_info['main_guard_calls'] + py_info['main_function_calls'])[:8]
        constants = py_info['constants']
        dataclass_classes = py_info['dataclass_classes']

        if py_info['has_main_guard'] and not (is_doc or is_benchmark or is_test):
            roles.append('entrypoint')
            reasons.append('__main__ guard')
        if py_info['has_dispatcher_pattern'] and not (is_doc or is_benchmark or is_test):
            roles.append('dispatcher')
            reasons.append('command dispatch pattern')
        if py_info['dataclass_classes'] and not (is_doc or is_benchmark):
            roles.append('model')
            reasons.append('dataclass model definitions')
    elif record.language in {'javascript', 'typescript', 'jsx', 'tsx'} and text:
        exports, classes = extract_js_ts_outline(text)

    entrypoint_name = re.search(r'(^|/)(main|app|server|manage|cli)\.[A-Za-z0-9]+$', rel_path)
    if entrypoint_name and not (is_doc or is_benchmark or is_test):
        roles.append('entrypoint')
        reasons.append('entrypoint-like filename')

    if re.search(r'\b(FastAPI|Flask|APIRouter|Blueprint|Typer|ArgumentParser|click\.command|uvicorn\.run|app\s*=\s*FastAPI)\b', text) and not (is_doc or is_benchmark):
        roles.append('entrypoint')
        reasons.append('runtime/app signal')

    if (functions or classes or exports) and not (is_doc or is_benchmark or is_test):
        roles.append('runtime_module')
        reasons.append('readable source module')

    if constants and 'config' in roles:
        reasons.append('module-level constants')

    env_vars = extract_env_var_names(text)
    if env_vars and 'config' not in roles:
        roles.append('config')
        reasons.append('environment variable usage')

    roles = dedupe_preserve_order(roles)
    reasons = dedupe_preserve_order(reasons)

    source_tier = 2
    if 'doc' in roles:
        source_tier = 0
    elif 'benchmark' in roles:
        source_tier = 1
    elif any(role in roles for role in ('config', 'test')):
        source_tier = 3
    elif any(role in roles for role in ('entrypoint', 'dispatcher', 'runtime_module', 'model', 'utility')):
        source_tier = 4

    priority = record.importance
    priority += source_tier * 18
    priority += len(reasons) * 8
    priority += min(len(env_vars), 5) * 4
    priority += min(len(functions) + len(classes) + len(exports), 6) * 2

    if 'entrypoint' in roles:
        priority += 60
    if 'dispatcher' in roles:
        priority += 32
    if 'config' in roles:
        priority += 18
    if 'model' in roles:
        priority += 12
    if 'test' in roles:
        priority += 6
    if 'doc' in roles:
        priority -= 90
    if 'benchmark' in roles:
        priority -= 50

    return {
        'record': record,
        'priority': priority,
        'source_tier': source_tier,
        'roles': roles,
        'reasons': reasons,
        'functions': functions,
        'classes': classes,
        'imports': imports,
        'exports': exports,
        'env_vars': env_vars,
        'main_calls': main_calls,
        'constants': constants,
        'dataclass_classes': dataclass_classes,
    }


def build_local_module_index(summaries: list[dict[str, Any]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for item in summaries:
        record = item['record']
        if record.language != 'python':
            continue
        for alias in python_module_aliases(record.rel_path):
            index.setdefault(alias, []).append(record.rel_path)
    for alias, paths in list(index.items()):
        index[alias] = dedupe_preserve_order(paths)
    return index


def resolve_local_import_paths(import_name: str, module_index: dict[str, list[str]]) -> list[str]:
    if import_name in module_index:
        return module_index[import_name]
    matched: list[str] = []
    prefix = import_name + '.'
    for alias, paths in module_index.items():
        if alias.startswith(prefix):
            matched.extend(paths)
    return dedupe_preserve_order(matched)


def build_record_summaries(inspection: InspectionResult) -> list[dict[str, Any]]:
    readable = [
        record for record in inspection.records
        if not (record.is_binary or record.is_large or record.is_unreadable) and record.redacted_text
    ]
    summaries = [summarize_record_for_context(record) for record in readable]
    module_index = build_local_module_index(summaries)
    inbound_counts: dict[str, int] = {}

    for item in summaries:
        target_paths: list[str] = []
        for import_name in item['imports']:
            target_paths.extend(resolve_local_import_paths(import_name, module_index))
        target_paths = dedupe_preserve_order(target_paths)
        for rel_path in target_paths:
            if rel_path == item['record'].rel_path:
                continue
            inbound_counts[rel_path] = inbound_counts.get(rel_path, 0) + 1

    for item in summaries:
        inbound_refs = inbound_counts.get(item['record'].rel_path, 0)
        item['inbound_refs'] = inbound_refs
        item['priority'] += inbound_refs * 14
        if inbound_refs:
            item['reasons'] = dedupe_preserve_order(item['reasons'] + [f'referenced by {inbound_refs} local module(s)'])

    summaries.sort(key=lambda item: (-item['priority'], item['record'].rel_path))
    return summaries


def build_repo_facts_section(config: RuntimeConfig, inspection: InspectionResult, summaries: list[dict[str, Any]] | None = None) -> str:
    summaries = summaries or build_record_summaries(inspection)
    readable_count = len(summaries)

    entry_candidates = [
        item for item in summaries
        if 'entrypoint' in item['roles'] and item['source_tier'] >= 3
    ][:8]
    runtime_candidates = [
        item for item in summaries
        if any(role in item['roles'] for role in ('entrypoint', 'dispatcher', 'runtime_module', 'model', 'utility')) and item['source_tier'] >= 3
    ][:14]
    config_candidates = [item for item in summaries if 'config' in item['roles']][:8]
    test_candidates = [item for item in summaries if 'test' in item['roles']][:8]
    doc_candidates = [item for item in summaries if 'doc' in item['roles']][:4]

    env_vars: list[str] = []
    for item in summaries[:60]:
        env_vars.extend(item['env_vars'])
    env_vars = dedupe_preserve_order(env_vars)[:20]

    parts = [
        '# RepoFacts',
        '',
        f'- Project root: `{config.project_root}`',
        f'- Readable files available to AI: {readable_count}',
        f'- Skipped binary files: {inspection.stats.skipped_binary}',
        f'- Skipped large files: {inspection.stats.skipped_large}',
        f'- Skipped unreadable files: {inspection.stats.skipped_unreadable}',
        '',
        '## Confirmed Entry Points',
        '',
    ]

    if entry_candidates:
        for item in entry_candidates:
            details: list[str] = []
            if item['functions']:
                details.append('functions: ' + ', '.join(item['functions'][:3]))
            if item['main_calls']:
                details.append('calls: ' + ', '.join(item['main_calls'][:4]))
            details.append('signals: ' + ', '.join(item['reasons'][:3]))
            parts.append(f"- `{item['record'].rel_path}` — {'; '.join(details)}")
    else:
        parts.append('- Not found in provided context.')

    parts.extend(['', '## Runtime-Critical Files', ''])
    if runtime_candidates:
        for item in runtime_candidates:
            detail_bits: list[str] = []
            if item['roles']:
                detail_bits.append('roles: ' + ', '.join(item['roles'][:4]))
            if item['classes']:
                detail_bits.append('classes: ' + ', '.join(item['classes'][:4]))
            if item['functions']:
                detail_bits.append('functions: ' + ', '.join(item['functions'][:4]))
            if item['imports']:
                detail_bits.append('imports: ' + ', '.join(item['imports'][:4]))
            if item['inbound_refs']:
                detail_bits.append(f"local refs: {item['inbound_refs']}")
            parts.append(f"- `{item['record'].rel_path}` — {'; '.join(detail_bits) if detail_bits else 'runtime-related file'}")
    else:
        parts.append('- Not found in provided context.')

    parts.extend(['', '## Configuration Surface', ''])
    if config_candidates:
        for item in config_candidates:
            details: list[str] = []
            if item['constants']:
                details.append('constants: ' + ', '.join(item['constants'][:6]))
            if item['env_vars']:
                details.append('env vars: ' + ', '.join(item['env_vars'][:6]))
            if item['imports']:
                details.append('imports: ' + ', '.join(item['imports'][:4]))
            parts.append(f"- `{item['record'].rel_path}` — {'; '.join(details) if details else 'config-related file'}")
    else:
        parts.append('- No dedicated config file was clearly identified.')

    if env_vars:
        parts.extend(['', '### Environment Variables Seen', ''])
        for name in env_vars:
            parts.append(f'- `{name}`')

    parts.extend(['', '## Test Surface', ''])
    if test_candidates:
        for item in test_candidates:
            details = []
            if item['functions']:
                details.append('functions: ' + ', '.join(item['functions'][:4]))
            if item['imports']:
                details.append('imports: ' + ', '.join(item['imports'][:4]))
            parts.append(f"- `{item['record'].rel_path}`{' — ' + '; '.join(details) if details else ''}")
    else:
        parts.append('- Not found in provided context.')

    if doc_candidates:
        parts.extend(['', '## Documentation And Intent Signals', ''])
        for item in doc_candidates:
            parts.append(f"- `{item['record'].rel_path}` — weaker evidence than runtime code; use mainly for project intent or declared philosophy")

    parts.extend([
        '',
        '## Context Rules',
        '',
        '- Runtime code, config, and tests outrank markdown docs and benchmark/meta files.',
        '- Prefer exact file paths and symbols over narrative summaries.',
        '- Treat docs as intent hints, not as proof of runtime behavior.',
        '- If a command or runtime detail is not explicit in code/config/tests, keep it as Likely or Not found in provided context.',
        '',
    ])

    return "\n".join(parts).rstrip() + "\n"


def context_priority(item: dict[str, Any]) -> tuple[int, int, str]:
    return (-item['source_tier'], -item['priority'], item['record'].rel_path)


def build_ai_context(config: RuntimeConfig, dump: DumpResult, inspection: InspectionResult) -> AIContextResult:
    summaries = build_record_summaries(inspection)
    repo_facts = build_repo_facts_section(config, inspection, summaries)
    readable = [item['record'] for item in summaries]
    full_text = normalize_ai_context_text(repo_facts.rstrip() + "\n\n" + dump.text.lstrip())

    if len(full_text) <= config.ai_max_context_chars:
        return AIContextResult(text=full_text, used_compact_mode=False, selected_files=len(readable))

    if config.ai_require_full_context:
        raise ValueError(
            f"Full repository context requires {len(full_text)} characters, which exceeds ai_max_context_chars={config.ai_max_context_chars}. "
            "Increase AI_MAX_CONTEXT_CHARS or reduce the repository scope."
        )

    summaries.sort(key=context_priority)
    tree = build_tree(config, [config.project_root / record.rel_path for record in inspection.records])
    parts = [
        '# CompactRepoContext',
        '',
        f'- Generated: {now_iso()}',
        f'- Project root: `{config.project_root}`',
        f'- Compact mode reason: repo facts + full dump exceed {config.ai_max_context_chars} characters',
        '',
        repo_facts.rstrip(),
        '',
        '## Repository Tree',
        '',
        '```text',
        tree,
        '```',
        '',
        '## Selected Files',
        '',
    ]
    used_chars = sum(len(part) + 1 for part in parts)
    selected_files = 0

    for item in summaries:
        if selected_files >= config.ai_max_selected_files:
            break
        record = item['record']
        summary_bits: list[str] = []
        if item['roles']:
            summary_bits.append('roles: ' + ', '.join(item['roles'][:4]))
        if item['functions']:
            summary_bits.append('functions: ' + ', '.join(item['functions'][:4]))
        if item['classes']:
            summary_bits.append('classes: ' + ', '.join(item['classes'][:4]))
        if item['imports']:
            summary_bits.append('imports: ' + ', '.join(item['imports'][:4]))
        if item['main_calls']:
            summary_bits.append('calls: ' + ', '.join(item['main_calls'][:4]))
        if item['env_vars']:
            summary_bits.append('env: ' + ', '.join(item['env_vars'][:5]))
        if item['inbound_refs']:
            summary_bits.append(f"local refs: {item['inbound_refs']}")
        summary_line = ('- ' + '; '.join(summary_bits) + '\n\n') if summary_bits else ''
        section = f"### {record.rel_path}\n\n{summary_line}```{record.language}\n{record.redacted_text}\n```\n\n"
        if used_chars + len(section) > config.ai_max_context_chars:
            continue
        parts.append(section.rstrip())
        parts.append('')
        used_chars += len(section)
        selected_files += 1

    compact_text = "\n".join(parts).rstrip() + "\n"
    return AIContextResult(text=normalize_ai_context_text(compact_text), used_compact_mode=True, selected_files=selected_files)


def build_artifact_specs() -> tuple[ArtifactSpec, ...]:
    return (
        ArtifactSpec(
            slug='01_repository_research',
            title='Artifact 01 — Repository Research Pack',
            output_name='01_repository_research.md',
            focus=ARTIFACT_FOCUS,
        ),
    )


def build_research_prompt(config: RuntimeConfig, spec: ArtifactSpec, context_text: str) -> str:
    sections = "\n".join(RESEARCH_PROMPT_SECTIONS) + "\n\n"
    rules = "Rules:\n" + "\n".join(f"- {rule}" for rule in RESEARCH_PROMPT_RULES) + "\n\n"
    return (
        f"Produce a dense Markdown artifact named `{spec.output_name}` with this exact structure:\n\n"
        f"# {spec.title}\n"
        f"{sections}"
        f"Goal:\n{RESEARCH_PROMPT_GOAL}\n\n"
        f"{rules}"
        "Repository context:\n\n"
        f"{context_text}"
    )


def build_artifact_bundle(artifacts: tuple[ArtifactResult, ...]) -> str:
    parts = ['# Research Artifact Bundle', '', 'This bundle is ordered for the aggregator: instruction-ready research first, repository context later in the final prompt.', '']
    for artifact in artifacts:
        parts.extend([
            f"## {artifact.title}",
            '',
            artifact.text.strip(),
            '',
        ])
    return "\n".join(parts).rstrip() + "\n"


def build_readme_prompt(config: RuntimeConfig, artifact_bundle_text: str, context_text: str) -> str:
    sections = "\n".join(README_PROMPT_SECTIONS) + "\n\n"
    rules = "Rules:\n" + "\n".join(f"- {rule}" for rule in README_PROMPT_RULES) + "\n\n"
    return (
        f"Produce a rich Markdown file named `{config.readme_name}` with this exact structure:\n\n"
        f"{sections}"
        f"{rules}"
        "Research artifact:\n\n"
        f"{artifact_bundle_text}\n\n"
        "Repository context:\n\n"
        f"{context_text}"
    )


def ai_endpoint_url(config: RuntimeConfig) -> str:
    base = normalize_base_url(config.ai_base_url)
    if config.ai_protocol == "chat_completions":
        return base if base.endswith("/chat/completions") else base + "/chat/completions"
    if config.ai_protocol == "responses":
        return base if base.endswith("/responses") else base + "/responses"
    raise ValueError(f"Unsupported ai_protocol: {config.ai_protocol}")


def build_chat_messages(config: RuntimeConfig, prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": config.ai_system_prompt},
        {"role": "user", "content": prompt},
    ]


def build_ai_request_payload(config: RuntimeConfig, prompt: str) -> dict[str, Any]:
    if config.ai_protocol == "chat_completions":
        payload: dict[str, Any] = {
            "model": config.ai_model,
            "messages": build_chat_messages(config, prompt),
            "max_tokens": config.ai_max_output_tokens,
        }
        if config.ai_temperature is not None:
            payload["temperature"] = config.ai_temperature
        return payload
    if config.ai_protocol == "responses":
        payload = {
            "model": config.ai_model,
            "input": prompt,
            "instructions": config.ai_system_prompt,
            "max_output_tokens": config.ai_max_output_tokens,
        }
        if config.ai_temperature is not None:
            payload["temperature"] = config.ai_temperature
        return payload
    raise ValueError(f"Unsupported ai_protocol: {config.ai_protocol}")


def build_ai_request(config: RuntimeConfig, prompt: str) -> AIRequest:
    payload = build_ai_request_payload(config, prompt)
    endpoint = ai_endpoint_url(config)
    return AIRequest(prompt=prompt, payload=payload, endpoint_url=endpoint)


# ============================================================================
# AI CLIENT
# ============================================================================


def extract_text_from_chat_completion(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"} and item.get("text"):
                chunks.append(str(item["text"]))
        return "\n".join(chunks).strip()
    return ""


def extract_text_from_responses_api(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    chunks: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                chunks.append(str(content["text"]))
    return "\n".join(chunks).strip()


def extract_ai_response_text(config: RuntimeConfig, data: dict[str, Any]) -> str:
    if config.ai_protocol == "chat_completions":
        return extract_text_from_chat_completion(data)
    if config.ai_protocol == "responses":
        return extract_text_from_responses_api(data)
    return ""


def strip_outer_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    if not lines[0].startswith("```"):
        return stripped
    if lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def default_request_headers(config: RuntimeConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.ai_api_key}",
        "Content-Type": "application/json",
    }
    if config.ai_provider_name.lower().startswith("zai"):
        headers["Accept-Language"] = "en-US,en"
    return headers


def call_ai(config: RuntimeConfig, request: AIRequest) -> AIResponse:
    payload_bytes = json.dumps(request.payload).encode("utf-8")
    http_request = urllib.request.Request(
        request.endpoint_url,
        data=payload_bytes,
        headers=default_request_headers(config),
        method="POST",
    )
    with urllib.request.urlopen(http_request, timeout=config.ai_request_timeout_seconds) as response:
        raw_text = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw_text)
    text = extract_ai_response_text(config, parsed)
    if not text:
        raise RuntimeError("AI response did not contain readable text output.")
    cleaned = strip_outer_markdown_fence(text)
    return AIResponse(text=cleaned.strip(), raw=parsed)



def call_ai_with_gap(
    config: RuntimeConfig,
    request: AIRequest,
    effective_ai_caller: Callable[[RuntimeConfig, AIRequest], AIResponse],
    last_call_finished_at: list[float | None],
    label: str,
    logger: Callable[[str], None] | None = None,
) -> AIResponse:
    if last_call_finished_at[0] is not None:
        elapsed = time.perf_counter() - last_call_finished_at[0]
        wait_seconds = config.ai_request_gap_seconds - elapsed
        if wait_seconds > 0:
            if logger:
                logger(f"[sumai] Waiting {wait_seconds:.2f}s before {label}")
            time.sleep(wait_seconds)

    try:
        response = effective_ai_caller(config, request)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_seconds = max(config.ai_request_gap_seconds + 4.0, 5.0)
            retry_after = None
            if getattr(exc, 'headers', None):
                retry_after = exc.headers.get('Retry-After')
            if retry_after:
                try:
                    retry_seconds = max(retry_seconds, float(retry_after))
                except ValueError:
                    pass
            if logger:
                logger(f"[sumai] HTTP 429 during {label}; retrying once after {retry_seconds:.1f}s")
            time.sleep(retry_seconds)
            response = effective_ai_caller(config, request)
        else:
            last_call_finished_at[0] = time.perf_counter()
            raise

    last_call_finished_at[0] = time.perf_counter()
    return response


def build_placeholder_readme(reason: str) -> str:
    return (
        "> ⚠️ **This document was generated by an AI summarizer "
        "([sumai](https://github.com/bakunin-dev/sumai)).** "
        "It reflects the repository state at the time of generation and may contain errors or omissions. "
        "Always verify against actual source code before making decisions.\n\n"
        "# ReadmeDev\n\n"
        "ReadmeDev generation was skipped.\n\n"
        f"Reason: {reason}\n\n"
        f"Open `{SCRIPT_NAME}` and edit the AI CONFIG block near the top of the file.\n"
    )


# ============================================================================
# PIPELINE
# ============================================================================


def record_stage(
    timings: dict[str, StageTiming],
    name: str,
    started: float,
    status: str,
    meta: dict[str, Any] | None = None,
) -> None:
    timings[name] = StageTiming(
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        status=status,
        meta=meta or {},
    )


def write_artifact_result(config: RuntimeConfig, artifact: ArtifactResult) -> pathlib.Path:
    path = config.artifact_dir / artifact.output_name
    atomic_write_text(path, artifact.text.rstrip() + "\n")
    return path


def write_artifact_manifest(config: RuntimeConfig, artifacts: tuple[ArtifactResult, ...]) -> None:
    manifest = {
        "generated_at": now_iso(),
        "artifact_dir": str(config.artifact_dir),
        "artifacts": [
            {
                "slug": artifact.slug,
                "title": artifact.title,
                "output_name": artifact.output_name,
            }
            for artifact in artifacts
        ],
    }
    atomic_write_json(config.artifact_dir / "manifest.json", manifest)


def run_pipeline(
    config: RuntimeConfig,
    ai_caller: Callable[[RuntimeConfig, AIRequest], AIResponse] | None = None,
    logger: Callable[[str], None] | None = log,
) -> PipelineResult:
    timings: dict[str, StageTiming] = {}
    discovery: DiscoveryResult | None = None
    inspection: InspectionResult | None = None
    dump: DumpResult | None = None
    ai_context: AIContextResult | None = None
    ai_request: AIRequest | None = None
    ai_response: AIResponse | None = None
    artifacts: tuple[ArtifactResult, ...] = tuple()

    os.chdir(config.project_root)
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    effective_ai_caller = ai_caller or call_ai

    started = time.perf_counter()
    discovery = discover_project_files(config, logger=logger)
    record_stage(timings, "discover_project_files", started, "ok", {
        "files": len(discovery.files),
        "backend": discovery.backend,
        "truncated": discovery.truncated,
    })

    if not discovery.files:
        started = time.perf_counter()
        dump = DumpResult(text="# CodebaseDump\n\nNo files found.\n", stats=FileStats())
        atomic_write_text(config.dump_path, dump.text)
        record_stage(timings, "write_dump", started, "ok", {"path": str(config.dump_path)})

        started = time.perf_counter()
        placeholder = build_placeholder_readme("No files found in project.")
        atomic_write_text(config.readme_path, placeholder)
        record_stage(timings, "write_readme", started, "ok", {"path": str(config.readme_path), "reason": "no_files"})
        return PipelineResult(return_code=0, timings=timings, discovery=discovery, dump=dump)

    started = time.perf_counter()
    inspection = inspect_project_files(config, discovery)
    record_stage(timings, "inspect_project_files", started, "ok", {
        "records": len(inspection.records),
        "included": inspection.stats.included,
    })

    if config.verbose_explain:
        if logger:
            logger("[sumai] --- File inclusion explainer ---")
        for record in inspection.records:
            reasons: list[str] = []
            if record.is_binary:
                reasons.append("binary file")
            elif record.is_large:
                reasons.append(f"file too large ({record.size} > {config.max_file_bytes} bytes)")
            elif record.is_unreadable:
                reasons.append("unreadable")
            else:
                reasons.append("included")
            if not (record.is_binary or record.is_large or record.is_unreadable):
                summaries = build_record_summaries(inspection)
                for s in summaries:
                    if s['record'].rel_path == record.rel_path:
                        if s['reasons']:
                            reasons.append("important: " + ", ".join(s['reasons'][:3]))
                        reasons.append(f"score={s['priority']}")
                        break
            if logger:
                logger(f"  {'INCL' if 'included' in reasons else 'EXCL'}: {record.rel_path} — {', '.join(reasons)}")
        if logger:
            logger("[sumai] --- End of explainer ---")

    started = time.perf_counter()
    dump = render_dump(config, discovery, inspection)
    record_stage(timings, "render_dump", started, "ok", {"chars": len(dump.text)})

    if config.write_dump:
        started = time.perf_counter()
        atomic_write_text(config.dump_path, dump.text)
        record_stage(timings, "write_dump", started, "ok", {"path": str(config.dump_path)})
    else:
        record_stage(timings, "write_dump", time.perf_counter(), "skipped", {"reason": "write_dump=False"})

    if not config.ai_enabled:
        if config.write_readme:
            started = time.perf_counter()
            placeholder = build_placeholder_readme("AI is disabled.")
            atomic_write_text(config.readme_path, placeholder)
            record_stage(timings, "write_readme", started, "ok", {"path": str(config.readme_path), "reason": "ai_disabled"})
        else:
            record_stage(timings, "write_readme", time.perf_counter(), "skipped", {"reason": "write_readme=False"})
        record_stage(timings, "build_ai_context", time.perf_counter(), "skipped", {"reason": "ai_disabled"})
        return PipelineResult(return_code=0, timings=timings, discovery=discovery, inspection=inspection, dump=dump)

    if not config.ai_api_key or config.ai_api_key == "PASTE_YOUR_API_KEY_HERE":
        if config.write_readme:
            started = time.perf_counter()
            placeholder = build_placeholder_readme("AI_API_KEY is not configured.")
            atomic_write_text(config.readme_path, placeholder)
            record_stage(timings, "write_readme", started, "ok", {"path": str(config.readme_path), "reason": "missing_api_key"})
        else:
            record_stage(timings, "write_readme", time.perf_counter(), "skipped", {"reason": "write_readme=False"})
        record_stage(timings, "build_ai_context", time.perf_counter(), "skipped", {"reason": "missing_api_key"})
        return PipelineResult(return_code=0, timings=timings, discovery=discovery, inspection=inspection, dump=dump)

    started = time.perf_counter()
    try:
        ai_context = build_ai_context(config, dump, inspection)
    except Exception as exc:
        placeholder = build_placeholder_readme(str(exc))
        started_write = time.perf_counter()
        atomic_write_text(config.readme_path, placeholder)
        record_stage(timings, "build_ai_context", started, "error", {"error": str(exc)})
        record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "context_error"})
        return PipelineResult(return_code=1, timings=timings, discovery=discovery, inspection=inspection, dump=dump)

    record_stage(timings, "build_ai_context", started, "ok", {
        "chars": len(ai_context.text),
        "used_compact_mode": ai_context.used_compact_mode,
        "selected_files": ai_context.selected_files,
    })

    if config.verbose_explain and logger:
        if ai_context.used_compact_mode:
            logger(f"[sumai] Compact mode: selected {ai_context.selected_files} files from {len(inspection.records)} total (limit: {config.ai_max_context_chars} chars)")
        else:
            logger(f"[sumai] Full context mode: {ai_context.selected_files} files ({len(ai_context.text)} chars)")

    last_call_finished_at: list[float | None] = [None]
    artifact_results: list[ArtifactResult] = []

    for spec in build_artifact_specs():
        prompt = build_research_prompt(config, spec, ai_context.text)
        request = build_ai_request(config, prompt)
        stage_name = f"agent_{spec.slug}"
        started = time.perf_counter()
        try:
            response = call_ai_with_gap(
                config,
                request,
                effective_ai_caller,
                last_call_finished_at,
                label=spec.slug,
                logger=logger,
            )
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            placeholder = build_placeholder_readme(f"HTTP {exc.code} during {spec.slug}: {error_body[:3000] or exc.reason}")
            started_write = time.perf_counter()
            atomic_write_text(config.readme_path, placeholder)
            record_stage(timings, stage_name, started, "error", {"kind": "http", "code": exc.code})
            record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "http_error"})
            return PipelineResult(
                return_code=1,
                timings=timings,
                discovery=discovery,
                inspection=inspection,
                dump=dump,
                ai_context=ai_context,
                ai_request=request,
                artifacts=tuple(artifact_results),
            )
        except urllib.error.URLError as exc:
            placeholder = build_placeholder_readme(f"Network error during {spec.slug}: {exc.reason}")
            started_write = time.perf_counter()
            atomic_write_text(config.readme_path, placeholder)
            record_stage(timings, stage_name, started, "error", {"kind": "network", "reason": str(exc.reason)})
            record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "network_error"})
            return PipelineResult(
                return_code=1,
                timings=timings,
                discovery=discovery,
                inspection=inspection,
                dump=dump,
                ai_context=ai_context,
                ai_request=request,
                artifacts=tuple(artifact_results),
            )
        except Exception as exc:
            placeholder = build_placeholder_readme(f"Unexpected error during {spec.slug}: {exc}")
            started_write = time.perf_counter()
            atomic_write_text(config.readme_path, placeholder)
            record_stage(timings, stage_name, started, "error", {"kind": "unexpected", "error": str(exc)})
            record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "unexpected_error"})
            return PipelineResult(
                return_code=1,
                timings=timings,
                discovery=discovery,
                inspection=inspection,
                dump=dump,
                ai_context=ai_context,
                ai_request=request,
                artifacts=tuple(artifact_results),
            )

        artifact = ArtifactResult(
            slug=spec.slug,
            title=spec.title,
            output_name=spec.output_name,
            prompt=prompt,
            text=response.text,
        )
        artifact_results.append(artifact)
        write_artifact_result(config, artifact)
        record_stage(timings, stage_name, started, "ok", {
            "prompt_chars": len(prompt),
            "response_chars": len(response.text),
            "path": str(config.artifact_dir / spec.output_name),
        })

    artifacts = tuple(artifact_results)
    write_artifact_manifest(config, artifacts)

    artifact_bundle_text = build_artifact_bundle(artifacts)

    started = time.perf_counter()
    final_prompt = build_readme_prompt(config, artifact_bundle_text, ai_context.text)
    ai_request = build_ai_request(config, final_prompt)
    record_stage(timings, "build_ai_request", started, "ok", {
        "prompt_chars": len(ai_request.prompt),
        "endpoint_url": ai_request.endpoint_url,
        "artifacts": len(artifacts),
    })

    started = time.perf_counter()
    aggregator_stage_name = "agent_readme_aggregator"
    try:
        ai_response = call_ai_with_gap(
            config,
            ai_request,
            effective_ai_caller,
            last_call_finished_at,
            label="aggregator",
            logger=logger,
        )
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""
        placeholder = build_placeholder_readme(f"HTTP {exc.code}: {error_body[:3000] or exc.reason}")
        started_write = time.perf_counter()
        atomic_write_text(config.readme_path, placeholder)
        record_stage(timings, aggregator_stage_name, started, "error", {"kind": "http", "code": exc.code})
        record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "http_error"})
        return PipelineResult(
            return_code=1,
            timings=timings,
            discovery=discovery,
            inspection=inspection,
            dump=dump,
            ai_context=ai_context,
            ai_request=ai_request,
            artifacts=artifacts,
        )
    except urllib.error.URLError as exc:
        placeholder = build_placeholder_readme(f"Network error: {exc.reason}")
        started_write = time.perf_counter()
        atomic_write_text(config.readme_path, placeholder)
        record_stage(timings, aggregator_stage_name, started, "error", {"kind": "network", "reason": str(exc.reason)})
        record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "network_error"})
        return PipelineResult(
            return_code=1,
            timings=timings,
            discovery=discovery,
            inspection=inspection,
            dump=dump,
            ai_context=ai_context,
            ai_request=ai_request,
            artifacts=artifacts,
        )
    except Exception as exc:
        placeholder = build_placeholder_readme(f"Unexpected error: {exc}")
        started_write = time.perf_counter()
        atomic_write_text(config.readme_path, placeholder)
        record_stage(timings, aggregator_stage_name, started, "error", {"kind": "unexpected", "error": str(exc)})
        record_stage(timings, "write_readme", started_write, "ok", {"path": str(config.readme_path), "reason": "unexpected_error"})
        return PipelineResult(
            return_code=1,
            timings=timings,
            discovery=discovery,
            inspection=inspection,
            dump=dump,
            ai_context=ai_context,
            ai_request=ai_request,
            artifacts=artifacts,
        )

    record_stage(timings, aggregator_stage_name, started, "ok", {})

    started = time.perf_counter()
    ai_warning = (
        "> ⚠️ **This document was generated by an AI summarizer "
        "([sumai](https://github.com/bakunin-dev/sumai)).** "
        "It reflects the repository state at the time of generation and may contain errors or omissions. "
        "Always verify against actual source code before making decisions.\n\n"
    )
    atomic_write_text(config.readme_path, ai_warning + ai_response.text.rstrip() + "\n")
    record_stage(timings, "write_readme", started, "ok", {"path": str(config.readme_path)})

    return PipelineResult(
        return_code=0,
        timings=timings,
        discovery=discovery,
        inspection=inspection,
        dump=dump,
        ai_context=ai_context,
        ai_request=ai_request,
        ai_response=ai_response,
        artifacts=artifacts,
    )


COMMANDS = ("all", "dump", "readme")


def parse_args() -> tuple[str, pathlib.Path, bool]:
    """Parse CLI arguments. Returns (command, project_root, verbose_explain)."""
    import sys as _sys
    args = _sys.argv[1:]
    command = "all"
    project_root = PROJECT_ROOT
    verbose_explain = VERBOSE_EXPLAIN

    # Extract command if first non-flag arg
    if args and not args[0].startswith("-"):
        command = args[0].lower()
        args = args[1:]
        if command not in COMMANDS:
            print(
                f"[sumai] Unknown command: {command!r}. "
                f"Valid commands: {', '.join(COMMANDS)}. Use --help for usage.",
                flush=True,
            )
            raise SystemExit(1)

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--root", "-r"):
            if i + 1 >= len(args):
                print("[sumai] Error: --root requires a path argument", flush=True)
                raise SystemExit(1)
            project_root = pathlib.Path(args[i + 1]).resolve()
            if not project_root.is_dir():
                print(
                    f"[sumai] Error: --root path does not exist or is not a directory: {project_root}",
                    flush=True,
                )
                raise SystemExit(1)
            i += 2
        elif arg in ("--explain", "-e"):
            verbose_explain = True
            i += 1
        elif arg in ("--help", "-h"):
            print(
                "Usage: python sumai.py [command] [--root PATH] [--explain]\n"
                "\n"
                "Commands:\n"
                "  all     Write CodebaseDump.md and ReadmeDev.md (default)\n"
                "  dump    Write CodebaseDump.md only — no AI call\n"
                "  readme  Write ReadmeDev.md only — AI call, dump not saved\n"
                "\n"
                "Options:\n"
                "  --root PATH, -r PATH   Project root to scan (default: directory of sumai.py)\n"
                "  --explain, -e          Explain why files are included/excluded/important\n"
                "  --help, -h             Show this message\n",
                flush=True,
            )
            raise SystemExit(0)
        else:
            print(f"[sumai] Unknown argument: {arg!r}. Use --help for usage.", flush=True)
            raise SystemExit(1)
    return command, project_root, verbose_explain


def main() -> int:
    command, project_root, verbose_explain = parse_args()
    config = build_runtime_config(project_root=project_root)

    if command == "dump":
        config = replace(config, ai_enabled=False, write_dump=True, write_readme=False, verbose_explain=verbose_explain)
    elif command == "readme":
        config = replace(config, ai_enabled=True, write_dump=False, write_readme=True, verbose_explain=verbose_explain)
    else:  # "all"
        config = replace(config, ai_enabled=True, write_dump=True, write_readme=True, verbose_explain=verbose_explain)

    result = run_pipeline(config=config, logger=log)
    if config.artifact_dir.exists():
        shutil.rmtree(config.artifact_dir)

    if result.return_code == 0:
        if command == "dump":
            log(f"[sumai] Done. Wrote {config.dump_name}.")
        elif command == "readme":
            log(f"[sumai] Done. Wrote {config.readme_name}.")
        else:
            log(f"[sumai] Done. Wrote {config.dump_name} and {config.readme_name}.")
    else:
        log("[sumai] Completed with errors.")
    return result.return_code


if __name__ == "__main__":
    raise SystemExit(main())
