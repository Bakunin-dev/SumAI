# SKILL: sumai — codebase summarizer

## What this tool does

`sumai.py` is a zero-dependency Python script that scans the current project and produces two files:

- `CodebaseDump.md` — full codebase snapshot (all text files, redacted, structured as markdown)
- `ReadmeDev.md` — AI-generated developer reference document (architecture, entrypoints, runtime flow, extension guide, gaps)

The script runs a two-pass LLM pipeline: a research agent collects grounded technical facts, then an aggregator writes the final `ReadmeDev.md`. Intermediate artifacts are cleaned up automatically.

---

## When to invoke sumai

| Situation | Command |
|---|---|
| User asks to "generate docs" / "update ReadmeDev" | `readme` or `all` |
| User asks to "dump the codebase" / "create a codebase snapshot" | `dump` |
| User asks to "run sumai" without specifics | `all` |
| No API key available but codebase snapshot needed | `dump` |
| ReadmeDev.md is missing or stale, dump already fresh | `readme` |
| Fresh start on an unfamiliar project | `all` |

Do **not** invoke sumai for routine coding tasks, file edits, or questions that don't require a full project overview.

---

## How to invoke

```bash
export MISTRAL_API_KEY=your_key_here

# Write CodebaseDump.md + ReadmeDev.md
python sumai.py all --root /path/to/project

# Write CodebaseDump.md only (no AI, no API key needed)
python sumai.py dump --root /path/to/project

# Write ReadmeDev.md only (AI call, dump not saved)
python sumai.py readme --root /path/to/project

# --root optional if sumai.py is in the project root
python sumai.py all
```

**Command summary:**

| Command | AI call | Writes CodebaseDump.md | Writes ReadmeDev.md |
|---|---|---|---|
| `all` | ✅ | ✅ | ✅ |
| `dump` | ❌ | ✅ | ❌ |
| `readme` | ✅ | ❌ | ✅ |

---

## Configuration (top of sumai.py)

| Variable | Default | What it controls |
|---|---|---|
| `AI_ENABLED` | `True` | Set `False` to skip LLM call, only write `CodebaseDump.md` |
| `AI_PROVIDER_NAME` | `mistral_small` | Label only, no functional effect |
| `AI_PROTOCOL` | `chat_completions` | `chat_completions` or `responses` (OpenAI) |
| `AI_BASE_URL` | Mistral endpoint | Base URL for the provider API |
| `AI_MODEL` | `mistral-small-2603` | Model name passed to the API |
| `AI_API_KEY` | from env | Read from `MISTRAL_API_KEY` env var by default |
| `AI_MAX_CONTEXT_CHARS` | `600_000` | Max chars sent to LLM |
| `AI_MAX_OUTPUT_TOKENS` | `8000` | Max tokens in LLM response |

Switching providers: edit `AI_BASE_URL`, `AI_MODEL`, and `AI_PROTOCOL` at the top of `sumai.py`. No other changes needed.

---

## Output files

| File | Description | When written |
|---|---|---|
| `CodebaseDump.md` | Full codebase as structured markdown. Every text file, binary/secret indicators, file tree. | Always |
| `ReadmeDev.md` | Developer reference doc. Architecture, entrypoints, commands, extension guide, known gaps. | When `AI_ENABLED = True` and API key is valid |

Both files are written atomically (temp file + rename). They are excluded from the scan so they don't feed back into themselves.

---

## What sumai skips

Automatically excluded from the scan:

- `node_modules`, `.git`, `__pycache__`, `.venv`, `dist`, `build`, and other standard noise dirs
- Binary files, images, fonts, archives, compiled artifacts
- Lock files (`package-lock.json`, `poetry.lock`, `yarn.lock`, etc.)
- Secret-looking files (`.env`, `*.pem`, `id_rsa`, `secrets.*`)
- Files over 300 KB

Secrets inside included text files are redacted before being sent to the LLM (API keys, tokens, passwords, database URLs, private key blocks).

---

## How to use the output

**`CodebaseDump.md`** — treat as the full project context. Paste into any AI chat, or attach to prompts that need complete codebase awareness.

**`ReadmeDev.md`** — treat as the authoritative developer reference for this project. Read it before answering architectural questions, planning changes, or onboarding to an unfamiliar codebase. It is grounded in actual code: if something isn't found in the repo, the doc says `Not found in provided context` rather than inventing details.

---

## Pipeline internals (for debugging)

```
discover_project_files()     → git ls-files or filesystem walk
inspect_project_files()      → read, binary-sniff, redact secrets
render_dump()                → write CodebaseDump.md
build_ai_context()           → select files by importance score, compact if needed
[for each ArtifactSpec]
  build_research_prompt()    → focused research prompt
  call_ai()                  → LLM research pass
build_readme_prompt()        → aggregator prompt with all research artifacts
call_ai()                    → LLM aggregator pass
atomic_write(ReadmeDev.md)   → final output
shutil.rmtree(artifacts/)    → cleanup
```

Stage timings are tracked internally. On error, a placeholder `ReadmeDev.md` is written with the error message.

---

## Freshness check

Before using `ReadmeDev.md` as context, check if it's stale:

```python
import pathlib, os
readme = pathlib.Path("ReadmeDev.md")
if not readme.exists():
    # run sumai
    pass
src_files = list(pathlib.Path(".").rglob("*.py"))
if src_files and readme.stat().st_mtime < max(f.stat().st_mtime for f in src_files):
    # ReadmeDev.md is older than newest source file — consider re-running sumai
    pass
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ReadmeDev.md` contains `AI_API_KEY is not configured` | Key not set | `export MISTRAL_API_KEY=...` |
| `HTTP 401` in placeholder | Wrong or expired API key | Check key validity |
| `ReadmeDev.md` is generic / invented | Context too large, compact mode triggered | Lower `AI_MAX_CONTEXT_CHARS` or reduce project size |
| Only `CodebaseDump.md` written | `AI_ENABLED = False` | Set `AI_ENABLED = True` |
| Script skips files you need | File matches exclusion rules | Check `EXCLUDED_DIR_NAMES`, `EXCLUDED_GLOBS` in config |
