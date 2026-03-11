#!/usr/bin/env python3
"""techdebt-rx — Debt-Aware Translation Layer (POC)

Standalone CLI script.
- SonarCloud: fetch open issues above severity threshold
- GitHub: fetch file content + 90-day churn
- Jira: search for upcoming work; dedupe; create ticket with ADF description
- OpenAI: generate audience-specific narrative (PM + CTO + decision log) in strict JSON

Dependencies: openai, requests, python-dotenv
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

# ---- utils ----

def log(msg: str) -> None:
    print(msg, flush=True)


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return str(v) if v is not None else ""


def parse_bool(s: str) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "y", "on"}


def iso_days_ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


def safe_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # keep a bit from head and tail to preserve context
    head = text[: limit // 2]
    tail = text[-(limit - len(head)) :]
    return head + "\n\n...<truncated>...\n\n" + tail


def strip_sonar_component(component: str) -> str:
    # Sonar component looks like: projectkey:src/path/to/file.py
    if ":" in component:
        return component.split(":", 1)[1]
    return component


def filename_stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if "." in base:
        return base.rsplit(".", 1)[0]
    return base


# ---- SonarCloud ----

def sonar_search_issues(
    token: str,
    project_key: str,
    severities: str,
    types: str,
    page_size: int,
) -> List[Dict[str, Any]]:
    url = "https://sonarcloud.io/api/issues/search"
    headers = {
        # PRD says Bearer; SonarCloud often also works with Basic token auth.
        "Authorization": f"Bearer {token}",
    }
    params = {
        "projectKeys": project_key,
        "types": types,
        "severities": severities,
        "statuses": "OPEN",
        "ps": page_size,
    }

    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code == 401 or r.status_code == 403:
        # fallback to Basic auth (token as username) if Bearer rejected
        r = requests.get(url, auth=(token, ""), params=params, timeout=30)

    r.raise_for_status()
    data = r.json()
    return data.get("issues", [])


# ---- GitHub ----

def gh_headers(pat: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
    }


def gh_get_file_content(api_base: str, repo: str, path: str, pat: str) -> Optional[str]:
    url = f"{api_base}/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(pat), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "")
    if not content_b64:
        return ""
    # GitHub includes newlines in base64
    raw = base64.b64decode(content_b64.encode("utf-8"))
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return raw.decode(errors="replace")


def gh_get_commit_churn(
    api_base: str,
    repo: str,
    path: str,
    pat: str,
    since_iso: str,
    page_cap: int,
) -> Tuple[int, int, Optional[str]]:
    """Return (commit_count, unique_authors, last_commit_date_iso).

    commit_count is capped by page_cap * 100; if cap is hit, we still return the counted value.
    """
    url = f"{api_base}/repos/{repo}/commits"
    per_page = 100
    commit_count = 0
    authors = set()
    last_date = None

    for page in range(1, page_cap + 1):
        params = {"path": path, "since": since_iso, "per_page": per_page, "page": page}
        r = requests.get(url, headers=gh_headers(pat), params=params, timeout=30)
        if r.status_code == 404:
            return (0, 0, None)
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        commit_count += len(items)
        for it in items:
            a = it.get("author") or {}
            if a.get("login"):
                authors.add(a["login"])
            # commit date
            c = (it.get("commit") or {}).get("committer") or {}
            d = c.get("date")
            if d and (last_date is None or d > last_date):
                last_date = d
        if len(items) < per_page:
            break

    return (commit_count, len(authors), last_date)


# ---- Jira ----

def jira_headers(email: str, token: str) -> Dict[str, str]:
    basic = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {basic}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_search(base_url: str, email: str, token: str, jql: str, fields: str = "summary") -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/rest/api/3/search"
    payload = {"jql": jql, "maxResults": 3, "fields": [f.strip() for f in fields.split(",") if f.strip()]}
    r = requests.post(url, headers=jira_headers(email, token), data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()


def jira_get_createmeta(base_url: str, email: str, token: str, project_key: str, issue_type: str) -> Dict[str, Any]:
    # Jira v3 create meta
    url = f"{base_url.rstrip('/')}/rest/api/3/issue/createmeta"
    params = {
        "projectKeys": project_key,
        "issuetypeNames": issue_type,
        "expand": "projects.issuetypes.fields",
    }
    r = requests.get(url, headers=jira_headers(email, token), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def jira_resolve_field_and_value(
    createmeta: Dict[str, Any], field_display_name: str, desired_value_name: str
) -> Tuple[Optional[str], Optional[Any]]:
    """Find the field key and the value object to set.

    Returns (fieldKey, valuePayload) where valuePayload is suitable for issue fields.
    """
    projects = createmeta.get("projects", [])
    for p in projects:
        for it in p.get("issuetypes", []):
            fields = it.get("fields", {})
            for field_key, field in fields.items():
                if str(field.get("name", "")).strip() != field_display_name:
                    continue
                allowed = field.get("allowedValues") or []
                # If there are allowed values, match by 'name'
                for av in allowed:
                    if str(av.get("name", "")).strip() == desired_value_name:
                        # For select-like fields Jira expects object with id, or full object
                        if av.get("id"):
                            return field_key, {"id": av["id"]}
                        return field_key, {"name": desired_value_name}
                # If no allowed values, attempt raw string
                return field_key, desired_value_name
    return None, None


def adf_doc(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "doc", "version": 1, "content": nodes}


def adf_heading(text: str, level: int = 2) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def adf_paragraph(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def adf_paragraph_rich(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "paragraph", "content": chunks}


def adf_text(text: str, bold: bool = False) -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "text", "text": text}
    if bold:
        node["marks"] = [{"type": "strong"}]
    return node


def adf_rule() -> Dict[str, Any]:
    return {"type": "rule"}


def adf_task_list(items: List[str]) -> Dict[str, Any]:
    return {
        "type": "taskList",
        "attrs": {"localId": str(int(time.time() * 1000))},
        "content": [
            {
                "type": "taskItem",
                "attrs": {"localId": f"{int(time.time() * 1000)}-{i}", "state": "TODO"},
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": it}]}],
            }
            for i, it in enumerate(items)
        ],
    }


def build_adf_description(
    pm: Dict[str, Any],
    cto: Dict[str, Any],
    decision: Dict[str, Any],
    source_line: str,
) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = []

    nodes.append(adf_heading("📋 For the Product Manager", level=2))
    nodes.append(adf_paragraph(pm.get("plain_english_summary", "")))
    nodes.append(adf_paragraph_rich([adf_text("Cost of delay:", bold=True)]))
    nodes.append(adf_paragraph(pm.get("cost_of_delay", "")))
    nodes.append(adf_paragraph_rich([adf_text("Recommendation:", bold=True)]))
    nodes.append(adf_paragraph(pm.get("recommendation", "")))
    nodes.append(adf_paragraph(f"💡 {pm.get('one_liner','')}") )

    nodes.append(adf_rule())

    nodes.append(adf_heading("🏗 For the Tech Lead / CTO", level=2))
    nodes.append(adf_paragraph_rich([adf_text("Architectural Risk: ", bold=True), adf_text(str(cto.get("architectural_risk", "")))]))
    nodes.append(adf_paragraph(cto.get("technical_summary", "")))
    nodes.append(adf_paragraph_rich([adf_text("Blast radius: ", bold=True), adf_text(cto.get("blast_radius", ""))]))
    nodes.append(adf_paragraph_rich([adf_text("Churn signal: ", bold=True), adf_text(cto.get("churn_signal", ""))]))
    nodes.append(adf_paragraph_rich([adf_text("Priority: ", bold=True), adf_text(str(cto.get("priority", "")))]))

    nodes.append(adf_rule())

    nodes.append(adf_heading("📝 Decision Log", level=2))
    nodes.append(adf_paragraph_rich([adf_text("What this code appears to do:", bold=True)]))
    nodes.append(adf_paragraph(decision.get("auto_context", "")))
    nodes.append(adf_paragraph_rich([adf_text("Open questions for the engineer:", bold=True)]))
    oq = decision.get("open_questions") or []
    oq = [str(x) for x in oq][:3]
    while len(oq) < 3:
        oq.append("(add question)")
    nodes.append(adf_task_list(oq))

    nodes.append(adf_rule())
    nodes.append(adf_paragraph(source_line))

    return adf_doc(nodes)


def jira_create_issue(
    base_url: str,
    email: str,
    token: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/rest/api/3/issue"
    r = requests.post(url, headers=jira_headers(email, token), data=json.dumps({"fields": fields}), timeout=30)
    if r.status_code >= 400:
        # surface full response
        raise RuntimeError(f"Jira create failed: HTTP {r.status_code} — {r.text}")
    return r.json()


# ---- OpenAI ----

def openai_generate(prompt: str) -> Dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(api_key=env("OPENAI_API_KEY", required=True))
    model = env("OPENAI_MODEL", "gpt-5.2")
    temperature = float(env("OPENAI_TEMPERATURE", "0.2"))
    max_output_tokens = int(env("OPENAI_MAX_OUTPUT_TOKENS", "900"))

    # Use Responses API via SDK if available; fall back to chat.completions if not.
    try:
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        text = resp.output_text
    except Exception:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_output_tokens,
        )
        text = resp.choices[0].message.content

    # Must be pure JSON
    text = text.strip()
    # Sometimes models wrap in ```json ...```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def build_llm_prompt(context: Dict[str, Any]) -> str:
    schema = {
        "jira_title": "...",
        "jira_priority": "High or Medium",
        "pm_section": {
            "plain_english_summary": "...",
            "cost_of_delay": "...",
            "recommendation": "...",
            "one_liner": "...",
        },
        "cto_section": {
            "architectural_risk": "Low|Medium|High|Critical",
            "technical_summary": "...",
            "blast_radius": "...",
            "churn_signal": "...",
            "priority": "Immediate|Next Sprint|Next Quarter|Monitor",
        },
        "decision_log": {
            "auto_context": "...",
            "open_questions": ["...", "...", "..."],
        },
    }

    # Strict instructions to return JSON only
    return (
        "You are a senior engineering manager writing audience-specific narratives about technical debt. "
        "Return ONLY valid JSON (no markdown, no code fences, no commentary). "
        "Use this exact JSON structure:\n"
        + json.dumps(schema, indent=2)
        + "\n\nCONTEXT:\n"
        + json.dumps(context, indent=2)
        + "\n\nRules:\n"
        "- PM section: plain English, no engineering jargon, be specific and reference the file name and what it does.\n"
        "- CTO section: precise technical language grounded in the provided code snippet and Sonar message.\n"
        "- Make cost-of-delay concrete using churn + upcoming work.\n"
        "- jira_title max 80 chars, plain English, no jargon.\n"
        "- jira_priority must be either High or Medium.\n"
    )


def map_priority_to_jira(jira_priority: str) -> str:
    # Map model output to Jira priority scheme
    jp = (jira_priority or "").strip().lower()
    if jp == "high":
        return "High"
    return "Medium"


def main() -> int:
    load_dotenv()

    sonar_token = env("SONAR_TOKEN", required=True)
    sonar_project_key = env("SONAR_PROJECT_KEY", required=True)
    severities = env("SONAR_SEVERITIES", "CRITICAL,MAJOR")
    types = env("SONAR_TYPES", "CODE_SMELL,BUG,VULNERABILITY")
    page_size = int(env("SONAR_PAGE_SIZE", "50"))

    gh_pat = env("GITHUB_PAT", required=True)
    gh_repo = env("GITHUB_REPO", required=True)
    gh_api_base = env("GITHUB_API_BASE", "https://api.github.com")
    lookback_days = int(env("GITHUB_DAYS_LOOKBACK", "90"))
    truncate_chars = int(env("GITHUB_FILE_TRUNCATE_CHARS", "3000"))
    commits_page_cap = int(env("GITHUB_COMMITS_PAGE_CAP", "3"))

    jira_base = env("JIRA_BASE_URL", required=True)
    jira_email = env("JIRA_EMAIL", required=True)
    jira_token = env("JIRA_API_TOKEN", required=True)
    jira_project = env("JIRA_PROJECT_KEY", required=True)
    jira_issue_type = env("JIRA_ISSUE_TYPE", "Story")
    jira_space_name = env("JIRA_SPACE_NAME", "My Kanban Project")
    jira_work_type_name = env("JIRA_WORK_TYPE_NAME", jira_issue_type)

    dry_run = parse_bool(env("DRY_RUN", "false"))

    issues = sonar_search_issues(sonar_token, sonar_project_key, severities, types, page_size)
    if not issues:
        log("No issues found above threshold")
        return 0

    # Prepare Jira createmeta for dynamic required fields
    createmeta = jira_get_createmeta(jira_base, jira_email, jira_token, jira_project, jira_issue_type)

    total = len(issues)
    for idx, issue in enumerate(issues, start=1):
        sonar_issue_key = issue.get("key")
        severity = issue.get("severity")
        itype = issue.get("type")
        message = issue.get("message")
        component = issue.get("component") or ""
        rule = issue.get("rule")
        creation_date = issue.get("creationDate")
        effort = issue.get("effort")

        file_path = strip_sonar_component(component)
        log(f"[{idx}/{total}] Processing: {file_path} ({severity} {itype})")

        # GitHub file content
        log(" → Fetching file content from GitHub... ")
        file_content = None
        try:
            file_content = gh_get_file_content(gh_api_base, gh_repo, file_path, gh_pat)
            if file_content is None:
                log(f"   GitHub file not found: {file_path} — proceeding without file context")
                file_content = ""
            else:
                file_content = safe_truncate(file_content, truncate_chars)
                log("   done")
        except Exception as e:
            log(f"   GitHub file fetch failed: {e} — proceeding without file context")
            file_content = ""

        # GitHub churn
        since_iso = iso_days_ago(lookback_days)
        churn_count, unique_authors, last_commit_date = 0, 0, None
        try:
            log(" → Fetching commit history... ")
            churn_count, unique_authors, last_commit_date = gh_get_commit_churn(
                gh_api_base, gh_repo, file_path, gh_pat, since_iso, commits_page_cap
            )
            log(f"   {churn_count} commits in {lookback_days} days")
        except Exception as e:
            log(f"   commit history failed: {e} — treating churn as 0")

        # Jira upcoming work search
        upcoming_count = 0
        upcoming_titles: List[str] = []
        try:
            log(" → Searching Jira for upcoming work... ")
            stem = filename_stem(file_path)
            jql = (
                f"project = {jira_project} "
                f"AND status in (\"To Do\", \"In Progress\") "
                f"AND text ~ \"{stem}\" "
                f"ORDER BY created DESC"
            )
            res = jira_search(jira_base, jira_email, jira_token, jql)
            issues2 = res.get("issues", [])
            upcoming_count = int(res.get("total", 0))
            for it in issues2[:3]:
                fields = it.get("fields", {})
                if fields.get("summary"):
                    upcoming_titles.append(fields["summary"])
            log(f"   {upcoming_count} matching tickets found")
        except Exception as e:
            log(f"   Jira search failed: {e} — treating upcoming as 0")

        # Dedupe
        try:
            dedupe_marker = f"SonarIssueKey: {sonar_issue_key}"
            jql = (
                f"project = {jira_project} AND labels = debt-translated "
                f"AND text ~ \"{dedupe_marker}\""
            )
            res = jira_search(jira_base, jira_email, jira_token, jql)
            if int(res.get("total", 0)) > 0:
                log(" → Creating Jira ticket... skipped (duplicate)")
                continue
        except Exception as e:
            # If dedupe fails, proceed (POC) but log it
            log(f"   Dedupe check failed: {e} — proceeding")

        # Debt age
        days_age = None
        try:
            if creation_date:
                created = dt.datetime.fromisoformat(creation_date.replace("Z", "+00:00"))
                days_age = (dt.datetime.now(dt.timezone.utc) - created).days
        except Exception:
            days_age = None

        ctx = {
            "sonar_issue_key": sonar_issue_key,
            "type": itype,
            "severity": severity,
            "message": message,
            "rule": rule,
            "effort": effort,
            "debt_age_days": days_age,
            "file_path": file_path,
            "file_content_truncated": file_content,
            "churn_count_90d": churn_count,
            "unique_authors_90d": unique_authors,
            "last_commit_date": last_commit_date,
            "upcoming_count": upcoming_count,
            "upcoming_titles": upcoming_titles,
        }

        # OpenAI
        log(" → Generating narrative with OpenAI... ")
        try:
            prompt = build_llm_prompt(ctx)
            out = openai_generate(prompt)
            log("   done")
        except Exception as e:
            log(f"   OpenAI failed: {e} — skipping issue")
            continue

        jira_title = str(out.get("jira_title", "Technical debt item"))[:80]
        jira_priority_model = str(out.get("jira_priority", "Medium"))
        jira_priority = map_priority_to_jira(jira_priority_model)
        pm_section = out.get("pm_section") or {}
        cto_section = out.get("cto_section") or {}
        decision_log = out.get("decision_log") or {}

        # Build ADF description
        source_line = (
            f"🔍 Source: SonarCloud Issue {sonar_issue_key} | Rule: {rule} | Severity: {severity} | "
            f"Debt age: {days_age if days_age is not None else 'unknown'} days | Remediation effort: {effort}\n"
            f"SonarIssueKey: {sonar_issue_key}"
        )
        adf = build_adf_description(pm_section, cto_section, decision_log, source_line)

        # Required custom fields (Space / Work type) resolved dynamically
        extra_fields: Dict[str, Any] = {}
        space_key, space_val = jira_resolve_field_and_value(createmeta, "Space", jira_space_name)
        if space_key and space_val is not None:
            extra_fields[space_key] = space_val

        wt_key, wt_val = jira_resolve_field_and_value(createmeta, "Work type", jira_work_type_name)
        if wt_key and wt_val is not None:
            extra_fields[wt_key] = wt_val

        fields = {
            "project": {"key": jira_project},
            "issuetype": {"name": jira_issue_type},
            "summary": jira_title,
            "priority": {"name": jira_priority},
            "labels": ["technical-debt", "debt-translated", f"sonar-{str(severity).lower()}"],
            "description": adf,
            **extra_fields,
        }

        log(" → Creating Jira ticket... ")
        if dry_run:
            log("   DRY_RUN=true — not creating ticket")
            continue

        try:
            created = jira_create_issue(jira_base, jira_email, jira_token, fields)
            key = created.get("key")
            log(f"   created {key}")
        except Exception as e:
            log(f"   Jira ticket creation fails — {e} — skipping")
            continue

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Interrupted")
        raise
    except Exception as e:
        log(f"Fatal: {e}")
        raise
