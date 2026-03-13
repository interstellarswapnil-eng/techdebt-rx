# techdebt-rx

A **debt-aware translation layer** (POC) that turns raw technical debt signals into **stakeholder-ready Jira tickets**.

It:
- Pulls **open issues from SonarCloud** (severity/type filtered)
- Pulls **code context + churn signals from GitHub** (file content + 90-day commit history)
- Looks for **near-term work in Jira** to estimate “cost of delay” and dedupe
- Uses **OpenAI** to generate a structured narrative (PM + CTO + decision log)
- Creates a **Jira ticket** with a rich **ADF description** (or plain text fallback)

This repo also includes a small **demo CSV → Jira importer** to seed epics/stories.

---

## Repo contents

- `debt_translator.py` — main CLI script (Sonar → GitHub → Jira → OpenAI → Jira)
- `create_jira_issues.py` — demo data importer that reads `techdebt-rx-jira-import.csv`
- `techdebt-rx-jira-import.csv` — sample epics + stories
- `requirements.txt` — Python deps
- `.env.example` — environment variable template

---

## Quickstart

### 1) Setup

```bash
cd techdebt-rx
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with your tokens and project details.

### 2) Run (dry run first)

Set:

```ini
DRY_RUN=true
```

Then:

```bash
python debt_translator.py
```

If the output looks sane, set `DRY_RUN=false` and run again to actually create tickets.

---

## Configuration (.env)

### SonarCloud

- `SONAR_TOKEN` — SonarCloud token
- `SONAR_PROJECT_KEY` — Sonar project key (e.g., `org_project`)
- `SONAR_SEVERITIES` — comma-separated, default: `CRITICAL,MAJOR`
- `SONAR_TYPES` — comma-separated, default: `CODE_SMELL,BUG,VULNERABILITY`
- `SONAR_PAGE_SIZE` — default: `50`

Notes:
- The script tries **Bearer token** auth first and falls back to **Basic** auth if needed.

### GitHub

- `GITHUB_PAT` — GitHub Personal Access Token
- `GITHUB_REPO` — `owner/repo`
- `GITHUB_API_BASE` — default: `https://api.github.com`
- `GITHUB_DAYS_LOOKBACK` — churn lookback window, default: `90`
- `GITHUB_FILE_TRUNCATE_CHARS` — truncate file content sent to the LLM, default: `3000`
- `GITHUB_COMMITS_PAGE_CAP` — caps commit pagination (each page = 100), default: `3`

### Jira (Cloud)

- `JIRA_BASE_URL` — e.g. `https://your-domain.atlassian.net`
- `JIRA_EMAIL` — Jira user email
- `JIRA_API_TOKEN` — Jira API token
- `JIRA_PROJECT_KEY` — e.g. `ABC`
- `JIRA_ISSUE_TYPE` — default: `Story`

**Team-managed field weirdness:**
- `JIRA_SPACE_NAME` — value to set for a field named **“Space”** (if present/required)
- `JIRA_WORK_TYPE_NAME` — value to set for a field named **“Work type”** (if present/required)

These are resolved dynamically via `createmeta`:
- The script fetches create-metadata and tries to find fields by display name and match allowed values.

Optional / advanced:
- `JIRA_API_VERSION` — `3` (default) or `2`
- `JIRA_DESCRIPTION_MODE` — `adf` (default) or `plain`

Why the toggles exist:
- Jira Cloud v3 prefers **ADF** (Atlassian Document Format) for `description`, but it can be picky.
- Jira v2 is sometimes more forgiving for `description` as a plain string.

### OpenAI

- `OPENAI_API_KEY`
- `OPENAI_MODEL` — default: `gpt-5.2`
- `OPENAI_TEMPERATURE` — default: `0.2`
- `OPENAI_MAX_OUTPUT_TOKENS` — default: `900`

### Behavior

- `DRY_RUN` — `true`/`false` (default `false`)

---

## What the translator actually does

For each Sonar issue:

1. **Fetch Sonar issue**: key, severity, type, message, rule, effort, creation date
2. **Infer file path**: Sonar components are typically like `projectkey:src/path/file.py`
3. **Fetch GitHub context**:
   - file content (truncated)
   - commit churn in last N days: commit count, unique authors, last commit date
4. **Search Jira for upcoming work**:
   - uses filename stem (e.g. `payments_service.py` → `payments_service`) to JQL search
   - counts “not done” tickets mentioning that stem
5. **Dedupe check**:
   - searches for existing translated tickets containing `SonarIssueKey: <key>`
6. **Generate narrative (OpenAI)**:
   - strict JSON schema output (PM section + CTO section + decision log)
7. **Create Jira issue**:
   - labels include: `technical-debt`, `debt-translated`, `sonar-<severity>`
   - description is ADF by default, with a “Source” footer including Sonar issue key

---

## Demo: import sample Epics + Stories

This is separate from the Sonar/GitHub/OpenAI pipeline — it’s just to quickly populate Jira with demo tickets.

```bash
pip install requests python-dotenv
cp .env.example .env
# fill in the Jira fields in .env
python create_jira_issues.py
```

What it does:
- Reads `techdebt-rx-jira-import.csv`
- Creates Epics first
- Creates Stories next and links them using `parent` (works for team-managed projects)

---

## Troubleshooting

### Jira rejects the description / INVALID_INPUT
Try:

1) Switch to plain descriptions:
```ini
JIRA_DESCRIPTION_MODE=plain
```

2) Switch Jira API version:
```ini
JIRA_API_VERSION=2
```

3) Verify required fields
Some Jira projects require additional custom fields on create. The script already attempts:
- `Space`
- `Work type`

If your project requires something else, you’ll need to add another `jira_resolve_field_and_value(...)` mapping.

### Dedupe misses tickets
Dedupe relies on the text marker:

```
SonarIssueKey: <sonar_key>
```

If someone edits the ticket description and removes it, dedupe won’t work.

### GitHub file not found
If Sonar’s component path doesn’t match the GitHub repo layout, the script proceeds without file context.

---

## Security / safety notes

- Treat `.env` as sensitive (it is gitignored).
- Start with `DRY_RUN=true`.
- The script can create *many* Jira tickets quickly; consider lowering `SONAR_PAGE_SIZE` while testing.

---

## Status

This is a **POC**. It’s intentionally pragmatic and biased toward “works in a real Jira/Sonar/GitHub setup” over being a polished library.
