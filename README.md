# SumAI

One file. Zero dependencies.

Drop it into any project and generate:

- `CodebaseDump.md` — a clean codebase snapshot for AI chats, reviews, and refactors
- `ReadmeDev.md` — a grounded developer doc generated from actual repository context

```bash
python sumai.py
````

Built for the real world of AI tooling: small models, free providers, slow APIs, and strict rate limits.

No pip install. No virtualenv. No config files. One Python file, standard library only.

---

## Why this exists

Most “AI documentation” workflows break down in practice:

* they assume you want a heavy setup
* they assume you always have access to a large paid model
* they ignore the reality of low RPM / RPS free-tier providers
* they produce vague summaries that feel detached from the actual codebase

SumAI takes a simpler approach:

* scan the repo
* build a structured code snapshot
* shape repository context carefully
* optionally call an LLM
* write a developer-facing doc that stays grounded in code

The point is not “use the biggest model possible.”
The point is to get useful output even when you are working with cheaper or free APIs.

---

## What it does

1. **Scans** your project — reads `.gitignore` / `.sumaiignore` patterns, skips binaries, redacts secrets
2. **Writes `CodebaseDump.md`** — a full repository snapshot in one markdown file, ready for AI chats or archiving
3. **Calls an LLM** (optional) — uses a two-pass pipeline: research pass → aggregator pass
4. **Writes `ReadmeDev.md`** — a grounded developer reference doc: architecture, entrypoints, runtime flow, extension guide, known gaps

This is designed to be practical, not flashy:
the local Python side is lightweight, while the expensive part is the API call itself.

---

## Quick start

```bash
# Set your API key
export MISTRAL_API_KEY=your_key_here

# Write both CodebaseDump.md and ReadmeDev.md (default)
python sumai.py all --root /path/to/your/project

# Write CodebaseDump.md only — no AI call, no API key needed
python sumai.py dump --root /path/to/your/project

# Write ReadmeDev.md only — AI call, dump not saved to disk
python sumai.py readme --root /path/to/your/project

# --root is optional if sumai.py is already in the project root
cd /your/project && python sumai.py all
```

**Output per command:**

```text
dump   → CodebaseDump.md
readme → ReadmeDev.md
all    → CodebaseDump.md + ReadmeDev.md
```

---

## Model strategy

SumAI is intentionally friendly to:

* **free providers**
* **cheap small models**
* **slow API backends**
* **low RPM / RPS limits**

It does not assume you are running a premium model on every call.

The project is structured so that useful output comes from:

* repository filtering
* context shaping
* grounded prompts
* small-model-compatible workflows

In many cases, a good small model is enough.

If you care about speed, cost, and not slamming provider limits, the default path should usually be a smaller model, not a giant one.

---

## AI Model Router

sumai uses a typed preset system. Most users only need one line:

```python
# Choose a preset (default: mistral_small)
AI_MODEL_PRESET = "mistral_small"
```

### Built-in presets

| Preset          | Provider | Protocol         | Model              |
| --------------- | -------- | ---------------- | ------------------ |
| `mistral_small` | Mistral  | chat_completions | mistral-small-2603 |
| `glm_flash`     | Z.ai     | chat_completions | glm-4.7-flash      |
| `openai_gpt5`   | OpenAI   | responses        | gpt-5              |

### Recommended usage

* **`mistral_small`** — the default recommendation for most users; fast enough, cheap enough, practical enough
* **`glm_flash`** — useful when you want a free-tier style workflow
* **`openai_gpt5`** — higher-end option when you care more about output quality than speed or cost

SumAI is not built around the assumption that bigger models are always the right answer.
For this workflow, a smaller model with well-shaped context is often the better tradeoff.

### Why small and free models matter here

This project is designed around a practical constraint:
API latency is often worse than the local script runtime.

That means the real bottleneck is usually:

* provider response time
* rate limits
* free-tier throughput

Not Python execution.

So the tool is intentionally conservative:

* it can run without AI at all
* it keeps the local side simple
* it supports cheaper and free providers natively
* it spaces requests instead of aggressively hammering APIs
* it tries to stay usable under weak provider conditions

### Add your own model

One entry in `MODEL_REGISTRY`:

```python
MODEL_REGISTRY["my_model"] = ModelSpec(
    provider_name="my_model",
    protocol="chat_completions",        # or "responses"
    base_url="https://api.example.com/v1",
    model="my-model-name",
    env_keys=("MY_API_KEY", "AI_API_KEY"),
)
```

Then set:

```python
AI_MODEL_PRESET = "my_model"
```

### Override mode

Override any preset value without changing the registry:

```python
AI_MODEL_PRESET = "mistral_small"
AI_MODEL_OVERRIDE = "mistral-small-latest"      # use different model
AI_BASE_URL_OVERRIDE = "https://my-proxy.com/v1" # custom endpoint
AI_API_KEY_OVERRIDE = "sk-..."                  # hardcoded key (not recommended)
```

Set `AI_ENABLED = False` to skip the LLM call and only generate `CodebaseDump.md`.

---

## Rate limits and free-tier reality

SumAI does not pretend free APIs behave like premium infrastructure.

It is built to tolerate the annoying stuff:

* low requests per minute
* low requests per second
* occasional `429` responses
* providers that answer in a few seconds on small models and much longer on larger ones

That is why the project favors:

* fewer AI calls over more orchestration
* compact repository context when needed
* a simple two-pass flow instead of a sprawling agent graph
* a default small-model path that is actually usable

If your provider is slow, that is usually a provider-side latency problem, not a sign that the local scanner is bloated.

---

## What gets skipped

sumai automatically ignores:

* `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, and other standard noise dirs
* Binary files, images, fonts, archives, compiled artifacts
* Lock files (`package-lock.json`, `poetry.lock`, etc.)
* Secret-looking files (`.env`, `*.pem`, `id_rsa`, etc.)
* Files over 300 KB

Secrets inside text files are redacted before being sent to the LLM:

* API keys
* tokens
* passwords
* private key blocks

---

## Commands

| Command  | AI call | CodebaseDump.md | ReadmeDev.md |
| -------- | ------- | --------------- | ------------ |
| `all`    | ✅       | ✅               | ✅            |
| `dump`   | ❌       | ✅               | ❌            |
| `readme` | ✅       | ❌               | ✅            |

### `dump`

Useful for:

* pasting the codebase into Claude / ChatGPT / Gemini manually
* code reviews and onboarding
* archiving a snapshot before a big refactor
* working with no API key at all

### `readme`

Useful when you want to regenerate the developer doc without overwriting an existing dump.

### `all`

Useful when you want the full workflow in one run.

---

## For AI agents (Claude Code, Cursor, etc.)

See [`SKILL.md`](./SKILL.md) — a machine-readable description of what sumai does and when to invoke it, designed for AI coding agents.

---

## What `ReadmeDev.md` contains

The generated developer doc covers:

* project skeleton and directory guide
* stack, tooling, test setup, ops signals
* entrypoints and runtime flow
* architecture layers and module relationships
* core data and domain model
* key commands and verification steps
* architectural invariants and safety boundaries
* configuration and environment
* extension guide
* known gaps and technical debt

Everything is meant to stay grounded in actual repository evidence.
If something is not clearly present in the repo, the generated doc should say so instead of inventing details.

---

## Requirements

* Python 3.10+
* an API key for your chosen LLM provider, only if `AI_ENABLED = True`

That’s it.

---

## License

MIT

```
```
