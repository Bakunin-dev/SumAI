---

name: sumai
description: Use when the user wants to create a Markdown snapshot of a codebase for AI review, onboarding, refactoring, or analysis.
-------------------------------------------------------------------------------------------------------------------------------------

# SumAI

SumAI is a single-file, zero-dependency Python script that collects useful repository files into one Markdown document.

It works locally:

* no AI API calls
* no HTTP requests
* no external dependencies

## Usage

Run from the project root:

```bash
python sumai.py
```

Scan another project:

```bash
python sumai.py --root /path/to/project
```

The default output is:

```text
snapcode_<project-folder>.md
```

Optional flags:

```bash
--output <filename>
--explain
--include-lockfiles
--include-generated
--include-all-configs
```

Use `--explain` when the user wants to see why files were included or skipped.

After running, report:

* output filename
* number of included files
* number of skipped files

Do not describe SumAI as an LLM tool or README generator. It only creates a deterministic codebase snapshot.
