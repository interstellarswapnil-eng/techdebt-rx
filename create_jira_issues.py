"""
Jira Issue Creator — TechDebt-RX Demo Data
==========================================
Reads the CSV and creates all Epics + Stories in Jira via API.
Epics are created first, then Stories are linked to them.

Usage:
    pip install requests python-dotenv
    python create_jira_issues.py
"""

import csv
import time
import os
from pathlib import Path
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
JIRA_BASE_URL   = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL      = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN  = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY"]
CSV_FILE        = "techdebt-rx-jira-import.csv"

AUTH    = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def create_issue(summary, issue_type, description, priority, labels, epic_id=None):
    """Create a single Jira issue and return its id and key."""
    fields = {
        "project":     {"key": JIRA_PROJECT_KEY},
        "summary":     summary,
        "issuetype":   {"name": issue_type},
        "description": {
            "type":    "doc",
            "version": 1,
            "content": [{
                "type":    "paragraph",
                "content": [{"type": "text", "text": description}]
            }]
        },
        "priority": {"name": priority},
        "labels":   [l.strip() for l in labels.split(",") if l.strip()],
    }

    # Link story to epic if we have the epic's issue key
    if epic_id:
        # Try the standard "Epic Link" field first (classic projects)
        # For next-gen/team-managed projects use "parent"
        fields["parent"] = {"key": epic_id}

    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json={"fields": fields},
        auth=AUTH,
        headers=HEADERS,
        timeout=15
    )

    if resp.status_code in (200, 201):
        data = resp.json()
        return data["key"]
    else:
        print(f"   ⚠️  Failed ({resp.status_code}): {resp.text[:200]}")
        return None


def get_issue_types():
    """Fetch available issue types for the project."""
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/project/{JIRA_PROJECT_KEY}",
        auth=AUTH,
        headers=HEADERS,
        timeout=10
    )
    if resp.status_code == 200:
        types = [it["name"] for it in resp.json().get("issueTypes", [])]
        return types
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  TechDebt-RX — Jira Demo Data Creator")
    print(f"  Project: {JIRA_PROJECT_KEY}  |  {JIRA_BASE_URL}")
    print("="*60 + "\n")

    # Check available issue types
    print("🔍 Checking project issue types...")
    issue_types = get_issue_types()
    print(f"   Available: {', '.join(issue_types)}\n")

    # Determine epic type name — could be "Epic" or "Epic" depending on project
    epic_type  = "Epic"  if "Epic"  in issue_types else issue_types[0]
    story_type = "Story" if "Story" in issue_types else "Task"

    print(f"   Using '{epic_type}' for Epics, '{story_type}' for Stories\n")

    # Read CSV
    csv_path = Path(CSV_FILE)
    if not csv_path.exists():
        print(f"❌ CSV file not found: {CSV_FILE}")
        print("   Make sure techdebt-rx-jira-import.csv is in the same folder as this script.")
        return

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # Separate epics and stories
    epics   = [r for r in rows if r["Issue Type"].strip() == "Epic"]
    stories = [r for r in rows if r["Issue Type"].strip() == "Story"]

    print(f"📋 Found {len(epics)} Epics and {len(stories)} Stories to create\n")

    # ── Step 1: Create Epics ──────────────────────────────────────────────────
    print("── Creating Epics ──────────────────────────────────────")
    epic_map = {}  # epic name → jira key

    for epic in epics:
        summary = epic["Summary"].strip()
        print(f"  + {summary}...", end=" ", flush=True)
        key = create_issue(
            summary    = summary,
            issue_type = epic_type,
            description= epic["Description"].strip(),
            priority   = epic["Priority"].strip(),
            labels     = epic["Labels"].strip(),
        )
        if key:
            epic_map[summary] = key
            print(f"✅ {key}")
        else:
            print("❌ failed")
        time.sleep(0.3)  # be kind to the API

    print(f"\n   Epic map: {epic_map}\n")

    # ── Step 2: Create Stories ────────────────────────────────────────────────
    print("── Creating Stories ────────────────────────────────────")
    created = 0
    failed  = 0

    for story in stories:
        summary    = story["Summary"].strip()
        epic_name  = story["Epic Link"].strip()
        epic_key   = epic_map.get(epic_name)

        print(f"  + {summary[:60]}{'...' if len(summary)>60 else ''}...", end=" ", flush=True)

        key = create_issue(
            summary    = summary,
            issue_type = story_type,
            description= story["Description"].strip(),
            priority   = story["Priority"].strip(),
            labels     = story["Labels"].strip(),
            epic_id    = epic_key,
        )
        if key:
            print(f"✅ {key}")
            created += 1
        else:
            print("❌ failed")
            failed += 1
        time.sleep(0.3)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print(f"  ✅ Created: {len(epic_map)} epics + {created} stories")
    if failed:
        print(f"  ⚠️  Failed:  {failed} stories")
    print(f"\n  View your board: {JIRA_BASE_URL}/jira/software/projects/{JIRA_PROJECT_KEY}/boards")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
