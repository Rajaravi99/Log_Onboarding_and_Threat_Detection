#!/usr/bin/env python3
import csv
import json
import subprocess
import sys
from datetime import datetime

import requests

# ---------- Configuration ----------
SUBSCRIPTION_ID = "<subscription-id>"
RESOURCE_GROUP = "<resource-group>"
WORKSPACE_NAME = "<workspace-name>"
API_VERSION = "2024-03-01"
DRY_RUN = True  # set to False to actually apply the changes

CSV_FILE = "rename_map.csv"
OLD_COL = "old_name"   # header of the column with current names
NEW_COL = "new_name"   # header of the column with new names

BASE_URL = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
    f"/resourceGroups/{RESOURCE_GROUP}"
    "/providers/Microsoft.OperationalInsights"
    f"/workspaces/{WORKSPACE_NAME}"
    "/providers/Microsoft.SecurityInsights/alertRules"
)


# ---------- CSV mapping ----------
def norm(text):
    """Normalise a name for matching: trim spaces, ignore case."""
    return text.strip().casefold()


def load_mapping(path):
    mapping = {}  # normalised old name -> (old name as written, new name)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        if OLD_COL not in headers or NEW_COL not in headers:
            sys.exit(f"CSV must have columns '{OLD_COL}' and '{NEW_COL}'. Found: {headers}")

        for line_no, row in enumerate(reader, start=2):
            old = (row[OLD_COL] or "").strip()
            new = (row[NEW_COL] or "").strip()
            if not old or not new:
                print(f"WARN line {line_no}: empty old or new name, ignored")
                continue
            key = norm(old)
            if key in mapping and mapping[key][1] != new:
                print(f"WARN line {line_no}: '{old}' appears twice with different new names; using the later one")
            mapping[key] = (old, new)
    return mapping


# ---------- Helpers ----------
def get_token():
    result = subprocess.run(
        [
            "az", "account", "get-access-token",
            "--resource", "https://management.azure.com/",
            "--query", "accessToken",
            "--output", "tsv",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def list_rules(headers):
    rules = []
    url = f"{BASE_URL}?api-version={API_VERSION}"
    while url:
        response = requests.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        rules.extend(data.get("value", []))
        url = data.get("nextLink")
    return rules


# ---------- Main ----------
def main():
    mapping = load_mapping(CSV_FILE)
    new_keys = {norm(new) for _, new in mapping.values()}
    print(f"Loaded {len(mapping)} mappings from {CSV_FILE}")

    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }

    rules = list_rules(headers)
    print(f"Found {len(rules)} rules in the workspace")

    backup_file = f"rules-backup-{datetime.now():%Y-%m-%d-%H%M}.json"
    with open(backup_file, "w") as f:
        json.dump(rules, f, indent=2)
    print(f"Backup saved to {backup_file}\n")

    matched = set()   # CSV rows that matched a rule
    planned = renamed = skipped = failed = 0

    for rule in rules:
        if rule.get("kind") not in ("Scheduled", "NRT"):
            print(f"SKIP (kind={rule.get('kind')}): {rule['name']}")
            skipped += 1
            continue

        old = rule["properties"]["displayName"]
        key = norm(old)

        if key in mapping:
            matched.add(key)
            new = mapping[key][1]
        elif key in new_keys:
            # the rule already carries a new name from the CSV
            for k, (_, n) in mapping.items():
                if norm(n) == key:
                    matched.add(k)
            print(f"SKIP (already renamed): {old}")
            skipped += 1
            continue
        else:
            print(f"SKIP (not in CSV): {old}")
            skipped += 1
            continue

        if old == new:
            print(f"SKIP (unchanged): {old}")
            skipped += 1
            continue

        print(f"RENAME: '{old}' -> '{new}'")
        planned += 1

        if DRY_RUN:
            continue

        body = dict(rule)
        body.pop("systemData", None)
        body["properties"] = dict(rule["properties"])
        body["properties"]["displayName"] = new

        put_url = f"{BASE_URL}/{rule['name']}?api-version={API_VERSION}"
        try:
            response = requests.put(put_url, headers=headers, json=body, timeout=60)
            if response.ok:
                renamed += 1
            else:
                failed += 1
                print(f"  FAILED ({response.status_code}): {response.text[:300]}")
        except requests.RequestException as exc:
            failed += 1
            print(f"  FAILED: {exc}")

    # CSV rows that never matched any rule (typos, deleted rules, wrong kind)
    unmatched = [orig for key, (orig, _) in mapping.items() if key not in matched]
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} CSV rows matched no Scheduled/NRT rule:")
        for name in unmatched:
            print(f"  - {name}")

    print(f"\nDone. planned={planned} renamed={renamed} skipped={skipped} failed={failed}")
    if DRY_RUN:
        print("DRY_RUN is True, so nothing was changed.")


if __name__ == "__main__":
    sys.exit(main())