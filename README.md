# SumAI

A single-file, zero-dependency Python utility that turns a repository into a clean Markdown snapshot for AI-assisted review, onboarding, refactoring, and architecture analysis.

The current version is fully local and deterministic:

- no AI or model API calls
- no HTTP requests
- no third-party Python packages
- no generated summaries or ranking heuristics

It collects handwritten source code, documentation, schemas, and small important configuration files into one file named `snapcode_<project-folder>.md`.

## Quick start

Copy `sumai.py` into the root of a project and run:

```bash
python sumai.py
```

Or scan another directory:

```bash
python sumai.py --root /path/to/project
```

The result is written inside the scanned project directory:

```text
snapcode_<project-folder>.md
```

## Commands

```bash
# Scan the directory containing sumai.py
python sumai.py

# Scan a specific project
python sumai.py --root /path/to/project

# Choose the output filename
python sumai.py --output repository_context.md

# Explain every include/skip decision in the generated dump
python sumai.py --explain

# Include package-manager lockfiles
python sumai.py --include-lockfiles

# Include generated files that pass the remaining checks
python sumai.py --include-generated

# Include generic JSON/YAML/TOML/INI/CFG files, not only important configs
python sumai.py --include-all-configs
```

Options can be combined:

```bash
python sumai.py \
  --root /path/to/project \
  --output project_context.md \
  --explain \
  --include-lockfiles
```

## What is included

By default, SumAI keeps files that are useful for understanding a codebase:

- source code across common programming languages
- Markdown, MDX, reStructuredText, and AsciiDoc documentation
- schemas such as GraphQL, Protocol Buffers, Prisma, SQL, JSON Schema, and Avro
- important project configuration such as `pyproject.toml`, `package.json`, Docker files, CI workflows, and common build configs
- selected manual text files such as `README`, `LICENSE`, `CHANGELOG`, `help.txt`, and `usage.txt`
- environment templates such as `.env.example`, with secret-aware redaction

The generated Markdown contains:

1. generation metadata and scan statistics
2. a repository tree
3. the included files in language-tagged code fences
4. a list of skipped files and reasons
5. optional per-file decisions when `--explain` is enabled

## What is skipped

The default policy excludes common noise and risky content, including:

- `.git`, virtual environments, caches, build output, dependencies, IDE state, and generated artifact directories
- images, media, archives, office documents, databases, model weights, compiled files, and other binary/data formats
- `.env`, credentials files, private keys, and secret-looking filenames
- package-manager lockfiles unless `--include-lockfiles` is used
- generated files unless `--include-generated` is used
- generic configuration files that are not recognized as important, unless `--include-all-configs` is used
- oversized files and content beyond the final dump size limit
- older SumAI/CodebaseDump context artifacts, preventing recursive dumps

Text content is additionally checked for common API keys, tokens, passwords, credential-bearing URLs, and private-key blocks. Detected values are replaced with redaction markers before output.

## Discovery behavior

When the target is inside a Git repository and Git is available, SumAI uses `git ls-files` to discover tracked and unignored files. Otherwise, it falls back to a filesystem scan.

Ignore patterns are read from:

- `.codebasedumpignore`
- `.sumaiignore`
- `.ignore`
- `.gitignore`

The built-in ignore parser intentionally supports a portable subset of gitignore syntax: comments, blank lines, directory patterns, and `fnmatch`-style globs. Negated `!` patterns are ignored.

## Safety limits

The script uses conservative limits to avoid accidentally producing enormous context files:

- separate per-file limits for code, docs, configs, schemas, and text
- a maximum of 25,000 discovered files
- a final dump cap of 5 MB
- binary sniffing before decoding file contents
- atomic output writes through a temporary file

Files rejected by these limits remain visible in the skipped-files section.

## Requirements

- Python 3.10 or newer
- Git is optional but recommended for repository-aware discovery

No installation step is required:

```bash
git clone https://github.com/Bakunin-dev/SumAI.git
cd SumAI
python sumai.py --root /path/to/project
```

## Typical uses

- paste a grounded repository snapshot into ChatGPT, Claude, Gemini, or another coding assistant
- prepare context for a code review or refactor
- create an onboarding artifact for a new developer
- inspect which files enter an AI context and why
- archive a readable codebase snapshot without external services

## License

MIT
