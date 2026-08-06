
"""
codebase_dump.py

One-file, zero-dependency CodebaseDump generator.

Goal:
- Drop this file into a repository root.
- Double-click it or run `python codebase_dump.py`.
- Get a clean `snapcode_<project-folder>.md` with handwritten source, docs, schemas, and small important configs.

What it intentionally does NOT do:
- No AI calls.
- No HTTP.
- No RepoContext / ranking / AST summarization.
- No dependencies outside Python stdlib.

Useful commands:
- python codebase_dump.py
- python codebase_dump.py --root /path/to/project
- python codebase_dump.py --explain
- python codebase_dump.py --include-lockfiles
- python codebase_dump.py --include-generated
- python codebase_dump.py --include-all-configs

Notes:
- .env is skipped, but .env.example/.env.sample/.env.template are included with env-aware redaction.
- Code fences are widened automatically, so markdown files with nested ``` blocks remain valid.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

# =============================================================================
# PRODUCT SETTINGS
# =============================================================================

OUTPUT_DUMP_PREFIX = "snapcode"
OUTPUT_DUMP_NAME = f"{OUTPUT_DUMP_PREFIX}_project.md"
GENERATOR_VERSION = "0.5.0"
SCRIPT_NAME = pathlib.Path(__file__).name
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent

PREFER_GIT_FILE_DISCOVERY = True

# Per-file limits. These are intentionally conservative: a useful LLM dump should
# not quietly absorb megabytes of generated output, cache, fixtures, or snapshots.
MAX_CODE_FILE_BYTES = 350_000
MAX_DOC_FILE_BYTES = 250_000
MAX_CONFIG_FILE_BYTES = 90_000
MAX_SCHEMA_FILE_BYTES = 180_000
MAX_TEXT_FILE_BYTES = 120_000
MAX_TREE_FILES = 25_000
BINARY_SNIFF_BYTES = 8_192

# A final hard cap for the output file. When this cap is reached, remaining file
# contents are skipped but listed in the skipped section.
MAX_TOTAL_DUMP_BYTES = 5_000_000

# =============================================================================
# ALLOW POLICY: things that are useful for understanding a codebase
# =============================================================================

CODE_SUFFIXES = {
    # Python
    ".py", ".pyi",
    # JavaScript / TypeScript / web frameworks
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".svelte", ".astro",
    # Web templates/styles
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Backend / compiled languages
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".php", ".rb", ".swift",
    ".scala", ".clj", ".cljs", ".ex", ".exs", ".erl", ".hrl",
    # C / C++ / Objective-C
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".m", ".mm",
    # Scripts
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    # Infra as code / query languages
    ".sql", ".graphql", ".gql", ".proto", ".prisma", ".tf",
    # 1C / OneScript
    ".bsl", ".os",
}

DOC_SUFFIXES = {".md", ".mdx", ".rst", ".adoc"}

SCHEMA_SUFFIXES = {
    ".graphql", ".gql", ".proto", ".prisma", ".sql",
    ".jsonschema", ".avsc",
}

# Generic config suffixes are NOT always included. They are included when the path
# looks important/manual, or when --include-all-configs is used.
CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}

IMPORTANT_CONFIG_FILENAMES = {
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
    "requirements-test.txt", "pipfile", "tox.ini", "pytest.ini", "mypy.ini", "ruff.toml",
    ".ruff.toml", ".python-version",
    # Node / web
    "package.json", "tsconfig.json", "tsconfig.base.json", "jsconfig.json",
    "vite.config.js", "vite.config.ts", "vite.config.mjs",
    "next.config.js", "next.config.ts", "next.config.mjs",
    "nuxt.config.js", "nuxt.config.ts", "svelte.config.js", "svelte.config.ts",
    "astro.config.js", "astro.config.ts",
    "tailwind.config.js", "tailwind.config.ts", "postcss.config.js", "postcss.config.cjs",
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", ".eslintrc", ".eslintrc.js",
    ".eslintrc.cjs", ".eslintrc.json", ".prettierrc", ".prettierrc.json", ".prettierrc.js",
    # Docker / build / general
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "makefile", "cmakelists.txt", ".editorconfig", ".gitignore", ".dockerignore",
    # Rust / Go / Java / .NET / PHP / Ruby
    "cargo.toml", "go.mod", "go.work", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "global.json", "composer.json", "gemfile",
    # CI / metadata
    ".gitlab-ci.yml", ".github/workflows", "renovate.json", "dependabot.yml",
    # AI/dev-tooling dotfiles that shape how agents inspect the repo
    ".claudeignore", "claude.md", ".codebasedumpignore", ".sumaiignore",
}

IMPORTANT_CONFIG_PATH_PARTS = {
    "config", "configs", "settings", "conf", ".github", "workflows", "ci", "deploy",
    "deployment", "docker", "k8s", "kubernetes", "helm", "charts",
}

TEXT_LIKE_FILENAMES = {
    "license", "licence", "copying", "notice", "authors", "contributors", "changelog",
    "changes", "todo", "readme", "contributing", "codeowners",
    "manual.txt", "help.txt", "usage.txt", "notes.txt", "todo.txt", "changelog.txt",
}

MANUAL_TEXT_GLOBS = {
    "manual.txt", "*_manual.txt", "*-manual.txt", "manual_*.txt",
    "*_manul.txt", "*-manul.txt", "check_*manual*.txt", "check_*manul*.txt",
    "help.txt", "usage.txt", "notes.txt", "todo.txt", "changelog.txt",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".vue": "vue", ".svelte": "svelte", ".astro": "astro",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".rs": "rust", ".go": "go", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".scala": "scala", ".clj": "clojure", ".cljs": "clojure",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hrl": "erlang",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".cs": "csharp", ".m": "objective-c", ".mm": "objective-cpp",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql", ".proto": "protobuf",
    ".prisma": "prisma", ".tf": "hcl",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".conf": "conf", ".xml": "xml",
    ".md": "markdown", ".mdx": "mdx", ".rst": "rst", ".adoc": "asciidoc",
    ".bsl": "bsl", ".os": "onescript",
}

SPECIAL_LANGUAGE_BY_NAME = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "cmakelists.txt": "cmake",
    "gemfile": "ruby",
    "pipfile": "toml",
}

# =============================================================================
# DENY POLICY: things that should not enter an LLM code dump by default
# =============================================================================

EXCLUDED_DIR_NAMES = {
    # VCS
    ".git", ".hg", ".svn", ".jj",
    # Python
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".venv", "venv", "env", ".env", ".ipynb_checkpoints", "htmlcov",
    # JS / web
    "node_modules", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
    ".vite", "dist", "build", "coverage",
    # Other ecosystems
    "target", "vendor", ".gradle", "out", "bin", "obj",
    # Infra / mobile
    ".terraform", ".serverless", ".aws-sam", "cdk.out", ".dart_tool", "pods", "deriveddata",
    # IDE / OS / generic cache
    ".idea", ".vscode", ".vs", ".cache", "cache", "tmp", "temp",
    # ML / AI / generated experiment artifacts
    "runs", "wandb", "mlruns", "lightning_logs", "checkpoints", "outputs", "artifacts",
}

EXCLUDED_FILE_NAMES = {
    OUTPUT_DUMP_NAME.lower(),
    "aicontext.md",
    ".ds_store", "thumbs.db", "desktop.ini",
    ".coverage", ".npmrc", ".pypirc", ".netrc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}

LOCK_FILENAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "poetry.lock", "pipfile.lock", "cargo.lock", "composer.lock", "gemfile.lock",
    "go.sum", "gradle.lockfile",
}

SECRET_FILENAMES = {
    ".env", ".env.local", ".env.development", ".env.production", ".env.test",
    "secrets.json", "secret.json", "secrets.yaml", "secret.yaml", "credentials.json",
}

ENV_TEMPLATE_FILENAMES = {
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    "env.example", "env.sample", "env.template",
}

# Old/generated context artifacts must never be absorbed into a new dump.
DUMP_ARTIFACT_GLOBS = {
    "CodebaseDump*.md", "RepoContext*.md", "AIContext*.md",
    "codebase_dump*.md", "repo_context*.md", "ai_context*.md",
    "snapcode_*.md",
}

BINARY_OR_DATA_SUFFIXES = {
    # Images / media / fonts
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico", ".svg",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Archives / documents
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    # Binary/build outputs
    ".pyc", ".pyo", ".pyd", ".jar", ".war", ".class", ".so", ".dylib", ".dll",
    ".exe", ".bin", ".obj", ".o", ".a", ".lib",
    # DB / datasets / model weights
    ".sqlite", ".sqlite3", ".db", ".pkl", ".pickle", ".npy", ".npz", ".parquet",
    ".pt", ".pth", ".onnx", ".safetensors", ".ckpt",
}

GENERATED_SUFFIXES = {
    ".min.js", ".min.css", ".map", ".bundle.js", ".bundle.css",
}

GENERATED_GLOBS = {
    "*.generated.*", "*.gen.*", "*_generated.*", "*_pb2.py", "*_pb2_grpc.py",
    "*.pb.go", "*.g.cs", "*.designer.cs", "*.designer.vb",
    "*.swagger.json", "openapi.generated.*",
}

NOISY_TEXT_GLOBS = {
    "*.log", "*.tmp", "*.temp", "*.swp", "*.swo", "*.bak", "*.backup", "*.old",
    "*.cache", "*.coverage", "*.lcov", "*.snap", "*.snapshot",
}

SIMPLE_IGNORE_FILES = (".codebasedumpignore", ".sumaiignore", ".ignore", ".gitignore")

# bytes that strongly suggest a non-text file
NON_TEXT_BYTES = set(range(0, 9)) | {11, 12} | set(range(14, 32))

# =============================================================================
# SECRET REDACTION
# =============================================================================

SENSITIVE_QUOTED_ASSIGNMENT_RE = re.compile(
    r'''(?im)^(\s*(?:export\s+)?["']?[\w.\-]*(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|database[_-]?url|db[_-]?url|connection[_-]?string|dsn|client[_-]?secret|private[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|authorization)[\w.\-]*["']?\s*[:=]\s*["'])([^"'\r\n]+)(["'])'''
)

SENSITIVE_UNQUOTED_ASSIGNMENT_RE = re.compile(
    r'''(?im)^(\s*(?:export\s+)?["']?[\w.\-]*(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|database[_-]?url|db[_-]?url|connection[_-]?string|dsn|client[_-]?secret|private[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|authorization)[\w.\-]*["']?\s*[:=]\s*)([^\s#"'\r\n]+)'''
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

# Extra guard for .env.example / env-like assignment blocks. These files are useful
# for agents because they show required settings, but teams sometimes accidentally
# put real keys into templates. Keep harmless defaults (ports, booleans, paths),
# redact secret-looking names and token-looking values.
ENV_ASSIGNMENT_RE = re.compile(
    r'''(?m)^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_.-]*)(\s*=\s*)(.*)$'''
)

ENV_SECRET_NAME_RE = re.compile(
    r'''(?ix)
    (?:
        api[_-]?key|apikey|secret|token|password|passwd|pwd|
        private[_-]?key|client[_-]?secret|
        access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|authorization|
        database[_-]?url|db[_-]?url|connection[_-]?string|dsn
    )
    '''
)

JWT_LIKE_RE = re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')

GENERATED_CONTENT_MARKERS = (
    "@generated",
    "auto-generated",
    "autogenerated",
    "automatically generated",
    "generated by",
    "code generated by",
    "do not edit",
    "do not modify",
    "this file was generated",
)

# =============================================================================
# DATA MODELS
# =============================================================================


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: pathlib.Path
    output_name: str = OUTPUT_DUMP_NAME
    script_name: str = SCRIPT_NAME
    prefer_git_file_discovery: bool = PREFER_GIT_FILE_DISCOVERY
    max_tree_files: int = MAX_TREE_FILES
    max_total_dump_bytes: int = MAX_TOTAL_DUMP_BYTES
    include_lockfiles: bool = False
    include_generated: bool = False
    include_all_configs: bool = False
    include_hidden_files: bool = True
    explain: bool = False
    simple_ignore_patterns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def output_path(self) -> pathlib.Path:
        return self.project_root / self.output_name


@dataclass(frozen=True)
class IncludeDecision:
    include: bool
    category: str
    language: str
    reason: str
    max_bytes: int = MAX_TEXT_FILE_BYTES


@dataclass(frozen=True)
class DiscoveredFile:
    path: pathlib.Path
    rel_path: str


@dataclass(frozen=True)
class FileRecord:
    rel_path: str
    abs_path: str
    size: int
    mtime_ns: int
    category: str
    language: str
    include: bool
    reason: str
    skipped_reason: str | None = None
    redacted_text: str | None = None


@dataclass(frozen=True)
class Stats:
    discovered: int = 0
    included: int = 0
    skipped_denied_dir: int = 0
    skipped_denied_name: int = 0
    skipped_ignored: int = 0
    skipped_not_allowed: int = 0
    skipped_binary_or_data: int = 0
    skipped_lockfile: int = 0
    skipped_generated: int = 0
    skipped_large: int = 0
    skipped_binary_sniff: int = 0
    skipped_unreadable: int = 0
    skipped_empty: int = 0
    skipped_total_cap: int = 0
    truncated_discovery: bool = False


@dataclass(frozen=True)
class DiscoveryResult:
    files: list[DiscoveredFile]
    backend: str
    truncated: bool
    stats: Stats


@dataclass(frozen=True)
class DumpResult:
    text: str
    records: list[FileRecord]
    stats: Stats
    duration_ms: float


# =============================================================================
# SMALL HELPERS
# =============================================================================


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


def posix_rel(root: pathlib.Path, path: pathlib.Path) -> str:
    return path.relative_to(root).as_posix()


def normalize_rel_path(rel_path: str) -> str:
    return rel_path.replace("\\", "/").lstrip("/")


def path_parts(rel_path: str) -> list[str]:
    return [part for part in normalize_rel_path(rel_path).split("/") if part]


def file_language(path_or_rel: pathlib.Path | str) -> str:
    path = pathlib.PurePosixPath(str(path_or_rel).replace("\\", "/"))
    name = path.name.lower()
    if name in SPECIAL_LANGUAGE_BY_NAME:
        return SPECIAL_LANGUAGE_BY_NAME[name]
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def bump(stats: Stats, field_name: str, amount: int = 1) -> Stats:
    return replace(stats, **{field_name: getattr(stats, field_name) + amount})


def read_text_lossy(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# =============================================================================
# IGNORE FILES
# =============================================================================


def load_simple_ignore_patterns(project_root: pathlib.Path) -> tuple[str, ...]:
    """Load simple ignore patterns.

    This is intentionally not a full gitignore engine. It supports the common useful
    subset for a portable zero-dependency script:
    - comments and empty lines
    - leading slash removal
    - directory patterns ending with /
    - fnmatch-style globs

    Negated patterns starting with ! are ignored; use .codebasedumpignore for direct control.
    """
    patterns: list[str] = []
    for name in SIMPLE_IGNORE_FILES:
        ignore_path = project_root / name
        if not ignore_path.exists() or not ignore_path.is_file():
            continue
        try:
            text = read_text_lossy(ignore_path)
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            normalized = normalize_rel_path(line)
            if normalized.endswith("/"):
                patterns.append(normalized + "**")
                patterns.append(normalized.rstrip("/"))
            else:
                patterns.append(normalized)
    return tuple(dict.fromkeys(patterns))


def matches_any_glob(rel_path: str, patterns: Iterable[str]) -> bool:
    rel = normalize_rel_path(rel_path)
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        normalized = normalize_rel_path(pattern)
        if fnmatch.fnmatch(rel, normalized) or fnmatch.fnmatch(name, normalized):
            return True
    return False


# =============================================================================
# PATH CLASSIFICATION
# =============================================================================


def has_denied_dir(rel_path: str) -> str | None:
    for part in path_parts(rel_path)[:-1]:
        if part.lower() in EXCLUDED_DIR_NAMES:
            return part
    return None


def is_hidden_noise_file(rel_path: str) -> bool:
    # Keep important dotfiles such as .gitignore/.editorconfig, but reject random hidden files.
    name = pathlib.PurePosixPath(rel_path).name
    lower = name.lower()
    if not name.startswith("."):
        return False
    if lower in IMPORTANT_CONFIG_FILENAMES or lower in TEXT_LIKE_FILENAMES:
        return False
    if lower.startswith(".env"):
        return True
    if lower in {".ds_store", ".coverage"}:
        return True
    # Hidden source files are rare; hidden configs are allowed only by explicit allowlist.
    return lower not in IMPORTANT_CONFIG_FILENAMES


def has_generated_suffix_or_name(rel_path: str) -> bool:
    lower = rel_path.lower()
    name = lower.rsplit("/", 1)[-1]
    if any(lower.endswith(suffix) for suffix in GENERATED_SUFFIXES):
        return True
    return matches_any_glob(name, GENERATED_GLOBS) or matches_any_glob(lower, GENERATED_GLOBS)


def is_dump_artifact(rel_path: str) -> bool:
    lower = normalize_rel_path(rel_path).lower()
    name = lower.rsplit("/", 1)[-1]
    return matches_any_glob(name, {pattern.lower() for pattern in DUMP_ARTIFACT_GLOBS})


def looks_like_named_config(rel_path: str) -> bool:
    lower = normalize_rel_path(rel_path).lower()
    name = lower.rsplit("/", 1)[-1]
    suffix = pathlib.PurePosixPath(name).suffix
    if suffix not in CONFIG_SUFFIXES:
        return False
    config_globs = {
        "config_*.*", "*_config.*", "*.config.*",
        "settings.*", "settings_*.*", "*_settings.*",
        "model.*", "model_*.*", "*_model.*",
        "models.*", "llm.*", "llm_*.*", "*_llm.*",
    }
    return matches_any_glob(name, config_globs)


def looks_like_manual_text(rel_path: str) -> bool:
    lower = normalize_rel_path(rel_path).lower()
    name = lower.rsplit("/", 1)[-1]
    return pathlib.PurePosixPath(name).suffix == ".txt" and matches_any_glob(name, MANUAL_TEXT_GLOBS)


def looks_like_important_config(rel_path: str) -> bool:
    lower = normalize_rel_path(rel_path).lower()
    name = lower.rsplit("/", 1)[-1]
    parts = set(path_parts(lower))

    if name in IMPORTANT_CONFIG_FILENAMES:
        return True
    if lower in IMPORTANT_CONFIG_FILENAMES:
        return True
    if lower.startswith(".github/workflows/") and pathlib.PurePosixPath(lower).suffix in {".yml", ".yaml"}:
        return True
    if looks_like_named_config(rel_path):
        return True
    if parts & IMPORTANT_CONFIG_PATH_PARTS:
        return True
    return False


def classify_path(config: RuntimeConfig, rel_path: str) -> IncludeDecision:
    rel = normalize_rel_path(rel_path)
    lower = rel.lower()
    name = lower.rsplit("/", 1)[-1]
    pure = pathlib.PurePosixPath(lower)
    suffix = pure.suffix

    denied_dir = has_denied_dir(rel)
    if denied_dir:
        return IncludeDecision(False, "denied_dir", "text", f"denied directory: {denied_dir}")

    if name == config.script_name.lower() or name == config.output_name.lower() or name in EXCLUDED_FILE_NAMES:
        return IncludeDecision(False, "denied_name", "text", "denied filename")

    if is_dump_artifact(rel):
        return IncludeDecision(False, "denied_name", "text", "old dump/context artifact")

    if matches_any_glob(lower, config.simple_ignore_patterns):
        return IncludeDecision(False, "ignored", "text", "matched ignore pattern")

    if name in ENV_TEMPLATE_FILENAMES:
        return IncludeDecision(True, "config", file_language(rel), "environment template file", MAX_CONFIG_FILE_BYTES)

    if name in SECRET_FILENAMES or name.startswith(".env"):
        return IncludeDecision(False, "secret_file", "text", "secret/env file")

    if not config.include_hidden_files and is_hidden_noise_file(rel):
        return IncludeDecision(False, "hidden", "text", "hidden file")
    if is_hidden_noise_file(rel):
        return IncludeDecision(False, "hidden_noise", "text", "hidden noise file")

    if name in LOCK_FILENAMES:
        if config.include_lockfiles:
            return IncludeDecision(True, "lockfile", file_language(rel), "lockfile included by flag", MAX_CONFIG_FILE_BYTES)
        return IncludeDecision(False, "lockfile", "text", "lockfile skipped by default")

    if suffix in BINARY_OR_DATA_SUFFIXES or any(lower.endswith(s) for s in BINARY_OR_DATA_SUFFIXES):
        return IncludeDecision(False, "binary_or_data", "text", f"binary/data suffix: {suffix or name}")

    if matches_any_glob(name, NOISY_TEXT_GLOBS) or matches_any_glob(lower, NOISY_TEXT_GLOBS):
        return IncludeDecision(False, "noisy_text", "text", "noisy text artifact")

    if has_generated_suffix_or_name(rel) and not config.include_generated:
        return IncludeDecision(False, "generated", file_language(rel), "generated filename pattern")

    language = file_language(rel)

    if suffix in CODE_SUFFIXES:
        return IncludeDecision(True, "code", language, f"source code suffix: {suffix}", MAX_CODE_FILE_BYTES)

    if suffix in DOC_SUFFIXES:
        return IncludeDecision(True, "docs", language, f"documentation suffix: {suffix}", MAX_DOC_FILE_BYTES)

    if suffix in SCHEMA_SUFFIXES:
        return IncludeDecision(True, "schema", language, f"schema suffix: {suffix}", MAX_SCHEMA_FILE_BYTES)

    if name in TEXT_LIKE_FILENAMES or looks_like_manual_text(rel):
        return IncludeDecision(True, "docs", "text", "important/manual text file", MAX_TEXT_FILE_BYTES)

    if name in IMPORTANT_CONFIG_FILENAMES or lower in IMPORTANT_CONFIG_FILENAMES:
        return IncludeDecision(True, "config", language, "important config filename", MAX_CONFIG_FILE_BYTES)

    if suffix in CONFIG_SUFFIXES:
        if config.include_all_configs:
            return IncludeDecision(True, "config", language, f"config suffix via --include-all-configs: {suffix}", MAX_CONFIG_FILE_BYTES)
        if looks_like_important_config(rel):
            return IncludeDecision(True, "config", language, "important config path/name", MAX_CONFIG_FILE_BYTES)
        return IncludeDecision(False, "not_allowed", language, "generic config not on allowlist")

    return IncludeDecision(False, "not_allowed", language, "not source/docs/schema/important config")


# =============================================================================
# FILE DISCOVERY
# =============================================================================


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

    files: list[DiscoveredFile] = []
    stats = Stats()
    truncated = False

    for raw_item in (item for item in proc.stdout.split(b"\x00") if item):
        rel = normalize_rel_path(raw_item.decode("utf-8", errors="replace"))
        stats = bump(stats, "discovered")
        path = config.project_root / rel
        if not path.exists() or not path.is_file() or path.is_symlink():
            continue
        files.append(DiscoveredFile(path=path, rel_path=rel))
        if len(files) >= config.max_tree_files:
            truncated = True
            break

    files.sort(key=lambda item: item.rel_path)
    return DiscoveryResult(files=files, backend="git", truncated=truncated, stats=replace(stats, truncated_discovery=truncated))


def walk_with_scandir(config: RuntimeConfig) -> DiscoveryResult:
    files: list[DiscoveredFile] = []
    stats = Stats()
    truncated = False

    def visit(directory: pathlib.Path) -> None:
        nonlocal stats, truncated
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
        except OSError:
            return

        for entry in entries:
            if len(files) >= config.max_tree_files:
                truncated = True
                return

            entry_path = pathlib.Path(entry.path)
            try:
                rel = posix_rel(config.project_root, entry_path)
            except ValueError:
                continue

            if not rel or entry.is_symlink():
                continue

            if entry.is_dir(follow_symlinks=False):
                if entry.name.lower() in EXCLUDED_DIR_NAMES:
                    # Count this as one denied directory; we intentionally do not enumerate children.
                    stats = bump(stats, "skipped_denied_dir")
                    continue
                if matches_any_glob(rel, config.simple_ignore_patterns):
                    stats = bump(stats, "skipped_ignored")
                    continue
                visit(entry_path)
                continue

            if entry.is_file(follow_symlinks=False):
                stats = bump(stats, "discovered")
                files.append(DiscoveredFile(path=entry_path, rel_path=normalize_rel_path(rel)))

    visit(config.project_root)
    files.sort(key=lambda item: item.rel_path)
    return DiscoveryResult(files=files, backend="filesystem", truncated=truncated, stats=replace(stats, truncated_discovery=truncated))


def discover_project_files(config: RuntimeConfig, logger: Callable[[str], None] | None = None) -> DiscoveryResult:
    if inside_git_repo(config):
        try:
            result = collect_files_with_git(config)
            if result.files:
                return result
        except Exception as exc:
            if logger:
                logger(f"[codebase-dump] Git discovery failed, falling back to filesystem scan: {exc}")
    return walk_with_scandir(config)


# =============================================================================
# TEXT / REDACTION / GENERATED DETECTION
# =============================================================================


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


def strip_wrapping_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'\"', "'"}:
        return stripped[1:-1]
    return stripped


def looks_like_secret_value(value: str) -> bool:
    candidate = strip_wrapping_quotes(value).strip()
    if not candidate:
        return False

    lowered = candidate.lower()
    if lowered in {"true", "false", "none", "null", "yes", "no"}:
        return False

    if JWT_LIKE_RE.search(candidate):
        return True

    # Known token formats are also handled by INLINE_SECRET_PATTERNS, but this
    # catches them before preserving comments/quotes in env-like lines.
    known_prefixes = (
        "sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "glpat-",
        "xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-", "akia", "aiza", "ya29.",
    )
    if lowered.startswith(known_prefixes):
        return True

    # Conservative high-entropy heuristic for env values only. Avoid redacting
    # normal paths, URLs without credentials, numbers, booleans, and readable text.
    if len(candidate) < 32:
        return False
    if any(sep in candidate for sep in ("\\", "/", " ", "\t")):
        return False
    has_alpha = bool(re.search(r"[A-Za-z]", candidate))
    has_digit = bool(re.search(r"\d", candidate))
    has_token_char = bool(re.search(r"[-_=+.]", candidate))
    return has_alpha and has_digit and has_token_char


def redacted_env_value(original_value: str) -> str:
    stripped = original_value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'\"', "'"}:
        quote = stripped[0]
        leading = original_value[: len(original_value) - len(original_value.lstrip())]
        trailing = original_value[len(original_value.rstrip()):]
        return f"{leading}{quote}[REDACTED]{quote}{trailing}"
    return "[REDACTED]"


def redact_env_like_assignments(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, name, eq, value = match.groups()
        # Preserve pure comments and harmless empty placeholders.
        if not value.strip():
            return match.group(0)
        if ENV_SECRET_NAME_RE.search(name) or looks_like_secret_value(value):
            return f"{prefix}{name}{eq}{redacted_env_value(value)}"
        return match.group(0)

    return ENV_ASSIGNMENT_RE.sub(repl, text)


def looks_like_runtime_secret_reference(value_token: str) -> bool:
    token = value_token.strip().lower()
    if not token:
        return False
    runtime_prefixes = (
        "os.getenv(", "getenv(", "os.environ", "environ.get(",
        "process.env", "system.getenv(", "settings.", "config.",
        "none", "null", "true", "false", "os.getenv", "getenv",
    )
    return token.startswith(runtime_prefixes)


def redact_unquoted_sensitive_assignment(match: re.Match[str]) -> str:
    prefix = match.group(1)
    value_token = match.group(2)
    if looks_like_runtime_secret_reference(value_token):
        return match.group(0)
    return f"{prefix}[REDACTED]"


def redact_sensitive_text(text: str, *, env_like: bool = False) -> str:
    redacted = PRIVATE_KEY_BLOCK_RE.sub("[REDACTED PRIVATE KEY BLOCK]", text)
    redacted = SENSITIVE_QUOTED_ASSIGNMENT_RE.sub(r"\1[REDACTED]\3", redacted)
    redacted = SENSITIVE_UNQUOTED_ASSIGNMENT_RE.sub(redact_unquoted_sensitive_assignment, redacted)
    for pattern in INLINE_SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: (match.group(1) + "[REDACTED]" + match.group(3))
            if match.lastindex and match.lastindex >= 3
            else "[REDACTED]",
            redacted,
        )
    return redact_env_like_assignments(redacted) if env_like else redacted


def looks_generated_by_content(text: str) -> bool:
    head = text[:8_000].lower()
    return any(marker in head for marker in GENERATED_CONTENT_MARKERS)


# =============================================================================
# INSPECTION
# =============================================================================


def inspect_single_file(config: RuntimeConfig, item: DiscoveredFile) -> FileRecord:
    decision = classify_path(config, item.rel_path)
    rel_path = item.rel_path

    try:
        stat = item.path.stat()
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns
    except OSError:
        return FileRecord(
            rel_path=rel_path,
            abs_path=str(item.path),
            size=0,
            mtime_ns=0,
            category=decision.category,
            language=decision.language,
            include=False,
            reason=decision.reason,
            skipped_reason="unreadable stat",
        )

    base = FileRecord(
        rel_path=rel_path,
        abs_path=str(item.path),
        size=size,
        mtime_ns=mtime_ns,
        category=decision.category,
        language=decision.language,
        include=decision.include,
        reason=decision.reason,
    )

    if not decision.include:
        return replace(base, skipped_reason=decision.reason)

    if size == 0:
        return replace(base, include=False, skipped_reason="empty file")

    if size > decision.max_bytes:
        return replace(base, include=False, skipped_reason=f"file too large: {size} > {decision.max_bytes} bytes")

    try:
        raw = item.path.read_bytes()
    except OSError:
        return replace(base, include=False, skipped_reason="unreadable file")

    if is_probably_binary_bytes(raw[:BINARY_SNIFF_BYTES]):
        return replace(base, include=False, skipped_reason="binary sniff")

    text = decode_text_bytes(raw)

    if not config.include_generated and looks_generated_by_content(text):
        return replace(base, include=False, skipped_reason="generated content marker")

    env_like = pathlib.PurePosixPath(rel_path.lower()).name in ENV_TEMPLATE_FILENAMES
    return replace(base, redacted_text=redact_sensitive_text(text, env_like=env_like))


def update_stats_from_record(stats: Stats, record: FileRecord) -> Stats:
    if record.include:
        return bump(stats, "included")

    reason = (record.skipped_reason or record.reason or "").lower()
    category = record.category

    if category == "denied_dir" or "denied directory" in reason:
        return bump(stats, "skipped_denied_dir")
    if category in {"denied_name", "secret_file", "hidden_noise"} or "denied filename" in reason:
        return bump(stats, "skipped_denied_name")
    if category == "ignored" or "ignore pattern" in reason:
        return bump(stats, "skipped_ignored")
    if category == "lockfile":
        return bump(stats, "skipped_lockfile")
    if category == "generated" or "generated" in reason:
        return bump(stats, "skipped_generated")
    if category == "binary_or_data" or "binary/data" in reason:
        return bump(stats, "skipped_binary_or_data")
    if "too large" in reason:
        return bump(stats, "skipped_large")
    if "binary sniff" in reason:
        return bump(stats, "skipped_binary_sniff")
    if "unreadable" in reason:
        return bump(stats, "skipped_unreadable")
    if "empty" in reason:
        return bump(stats, "skipped_empty")
    return bump(stats, "skipped_not_allowed")


def inspect_project_files(config: RuntimeConfig, discovery: DiscoveryResult) -> tuple[list[FileRecord], Stats]:
    records: list[FileRecord] = []
    stats = discovery.stats
    seen: set[str] = set()

    for item in discovery.files:
        if item.rel_path in seen:
            continue
        seen.add(item.rel_path)
        record = inspect_single_file(config, item)
        stats = update_stats_from_record(stats, record)
        records.append(record)

    records.sort(key=lambda record: record.rel_path)
    return records, stats


# =============================================================================
# RENDERING
# =============================================================================


def build_tree(project_name: str, rel_paths: Iterable[str]) -> str:
    tree: dict[str, dict] = {}
    for rel_path in rel_paths:
        node = tree
        for part in path_parts(rel_path):
            node = node.setdefault(part, {})

    lines = [project_name + "/"]

    def render(node: dict[str, dict], prefix: str = "") -> None:
        names = sorted(node.keys(), key=str.lower)
        for index, name in enumerate(names):
            is_last = index == len(names) - 1
            connector = "└── " if is_last else "├── "
            lines.append(prefix + connector + name)
            extension = "    " if is_last else "│   "
            render(node[name], prefix + extension)

    render(tree)
    return "\n".join(lines)


def render_stats(stats: Stats, backend: str, records: list[FileRecord]) -> list[str]:
    skipped_total = len([record for record in records if not record.include])
    return [
        f"- Discovery backend: `{backend}`",
        f"- Files discovered: {stats.discovered}",
        f"- Files included: {stats.included}",
        f"- Files skipped: {skipped_total}",
        f"- Skipped denied directories: {stats.skipped_denied_dir}",
        f"- Skipped denied names/secrets/hidden noise: {stats.skipped_denied_name}",
        f"- Skipped by ignore patterns: {stats.skipped_ignored}",
        f"- Skipped not allowed by whitelist: {stats.skipped_not_allowed}",
        f"- Skipped binary/data suffixes: {stats.skipped_binary_or_data}",
        f"- Skipped lockfiles: {stats.skipped_lockfile}",
        f"- Skipped generated files: {stats.skipped_generated}",
        f"- Skipped large files: {stats.skipped_large}",
        f"- Skipped binary sniff: {stats.skipped_binary_sniff}",
        f"- Skipped unreadable: {stats.skipped_unreadable}",
        f"- Skipped empty: {stats.skipped_empty}",
        f"- Skipped due to total dump cap: {stats.skipped_total_cap}",
        f"- Discovery truncated: {stats.truncated_discovery}",
    ]


def render_explain_section(records: list[FileRecord]) -> str:
    lines = ["## File Decisions", ""]
    for record in records:
        status = "INCLUDE" if record.include else "SKIP"
        reason = record.reason if record.include else (record.skipped_reason or record.reason)
        size = f"{record.size} bytes"
        lines.append(f"- {status}: `{record.rel_path}` — {record.category}; {reason}; {size}")
    return "\n".join(lines).rstrip() + "\n"


def render_skipped_section(records: list[FileRecord]) -> str:
    skipped = [record for record in records if not record.include]
    if not skipped:
        return "## Skipped Files\n\n- None.\n"

    lines = ["## Skipped Files", ""]
    for record in skipped:
        reason = record.skipped_reason or record.reason
        lines.append(f"- `{record.rel_path}` — {reason}")
    return "\n".join(lines).rstrip() + "\n"


def markdown_fence_for_text(text: str) -> str:
    """Return a fence longer than any backtick run inside the file content."""
    longest = 0
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    return "`" * max(3, longest + 1)


def render_file_section(record: FileRecord) -> str:
    text = record.redacted_text or ""
    fence = markdown_fence_for_text(text)
    language = record.language or "text"
    return "\n".join([
        f"### {record.rel_path}",
        "",
        f"{fence}{language}",
        text,
        fence,
        "",
    ])


def render_dump(config: RuntimeConfig, discovery: DiscoveryResult, records: list[FileRecord], stats: Stats) -> tuple[str, Stats]:
    included_records = [record for record in records if record.include]
    tree_paths = [record.rel_path for record in included_records]
    project_name = config.project_root.name or "project"

    parts: list[str] = [
        "# CodebaseDump",
        "",
        f"- Generated: {now_iso()}",
        f"- Generator: SumAi CodebaseDump v{GENERATOR_VERSION}",
        f"- Project: `{project_name}`",
    ]
    parts.extend(render_stats(stats, discovery.backend, records))
    parts.extend([
        "",
        "## Repository Tree",
        "",
        "```text",
        build_tree(project_name, tree_paths),
        "```",
        "",
        "## Files",
        "",
    ])

    used_bytes = len("\n".join(parts).encode("utf-8"))
    final_records: list[FileRecord] = []
    capped_stats = stats

    for record in records:
        if not record.include:
            final_records.append(record)
            continue

        section = render_file_section(record)
        section_bytes = len(section.encode("utf-8"))
        if used_bytes + section_bytes > config.max_total_dump_bytes:
            capped = replace(
                record,
                include=False,
                redacted_text=None,
                skipped_reason=f"total dump cap reached: {config.max_total_dump_bytes} bytes",
            )
            final_records.append(capped)
            capped_stats = bump(capped_stats, "skipped_total_cap")
            capped_stats = replace(capped_stats, included=max(0, capped_stats.included - 1))
            continue

        parts.append(section.rstrip())
        parts.append("")
        used_bytes += section_bytes
        final_records.append(record)

    parts.append(render_skipped_section(final_records).rstrip())
    parts.append("")

    if config.explain:
        parts.append(render_explain_section(final_records).rstrip())
        parts.append("")

    # Re-render header stats if total cap changed anything.
    if capped_stats != stats:
        parts = [
            "# CodebaseDump",
            "",
            f"- Generated: {now_iso()}",
            f"- Generator: SumAi CodebaseDump v{GENERATOR_VERSION}",
            f"- Project: `{project_name}`",
            *render_stats(capped_stats, discovery.backend, final_records),
            "",
            "## Repository Tree",
            "",
            "```text",
            build_tree(project_name, [record.rel_path for record in final_records if record.include]),
            "```",
            "",
            "## Files",
            "",
            *parts[parts.index("## Files") + 2:],
        ]

    return "\n".join(parts).rstrip() + "\n", capped_stats


# =============================================================================
# PIPELINE / CLI
# =============================================================================


def build_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    root = pathlib.Path(args.root).resolve() if args.root else PROJECT_ROOT.resolve()
    if not root.is_dir():
        raise SystemExit(f"[codebase-dump] Error: root path does not exist or is not a directory: {root}")

    config = RuntimeConfig(
        project_root=root,
        output_name=args.output or f"{OUTPUT_DUMP_PREFIX}_{root.name or 'project'}.md",
        include_lockfiles=args.include_lockfiles,
        include_generated=args.include_generated,
        include_all_configs=args.include_all_configs,
        explain=args.explain,
    )
    return replace(config, simple_ignore_patterns=load_simple_ignore_patterns(root))


def run_pipeline(config: RuntimeConfig, logger: Callable[[str], None] | None = log) -> DumpResult:
    started = time.perf_counter()
    discovery = discover_project_files(config, logger=logger)
    records, stats = inspect_project_files(config, discovery)
    text, stats = render_dump(config, discovery, records, stats)
    atomic_write_text(config.output_path, text)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return DumpResult(text=text, records=records, stats=stats, duration_ms=duration_ms)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codebase_dump.py",
        description="Generate a clean CodebaseDump.md from source/docs/small important configs.",
    )
    parser.add_argument("--root", "-r", default=None, help="Project root to scan. Default: directory of this script.")
    parser.add_argument("--output", "-o", default=None, help="Output markdown filename. Default: snapcode_<project-folder>.md")
    parser.add_argument("--include-lockfiles", action="store_true", help="Include package-manager lockfiles when they pass size/text checks.")
    parser.add_argument("--include-generated", action="store_true", help="Include generated files when they pass size/text checks.")
    parser.add_argument("--include-all-configs", action="store_true", help="Include generic json/yaml/toml/ini/cfg files, not only important configs.")
    parser.add_argument("--explain", "-e", action="store_true", help="Add a File Decisions section to the dump.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = build_runtime_config(args)
    result = run_pipeline(config=config, logger=log)

    log(
        f"[codebase-dump] Done. Wrote {config.output_path.name}. "
        f"Included {result.stats.included} file(s), skipped "
        f"{len([record for record in result.records if not record.include])} file(s), "
        f"{result.duration_ms} ms."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
