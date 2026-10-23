"""Write the agent's results back to the repo — idempotently, dry-run by default.

    python3 writeback.py                    # DRY RUN: show what would be written (default)
    python3 writeback.py --apply            # update issues/*.json + write compliance-issues/*.md
    python3 writeback.py --apply --github   # ...and open real GitHub issues for violations (gh CLI)

What it writes
- Triage (out/triage/<id>.json) -> issues/<id>.json: sets severity, adds labels, appends ONE comment
  from "always-on-agent". The comment carries a marker (hash of the record); if a comment with the same
  marker already exists, nothing is written. A changed record gets one new comment.
- Compliance (out/compliance/*.json) -> one issue per finding with status "violation", rendered from
  templates/violation_issue.md (Dev 2) or a built-in template. Local mode writes
  compliance-issues/<vendor>__<rule>.md; --github opens a GitHub issue on the gh default repo, skipping
  any issue whose title already exists. "needs_review" findings are reported, never opened as issues.

It never runs runbook commands, deploys, rolls back or pages anyone — those stay recommendations.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT = "always-on-agent"
DEFAULT_TEMPLATE = """[Compliance] {vendor_title} — {rule_title} violation

**Contract:** `{contract}` ({clause_ref})
**Policy rule:** {rule_title}

> {quote}

**Policy:** {policy_quote}

**Why this is a violation:** {reason}

**Suggested fix:** {suggested_fix}

_Opened by the Always-On Ops Agent. Data is synthetic._
"""


class _Blank(dict):
    def __missing__(self, key):
        return "—"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker(kind, record):
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:12]
    return f"{kind}:{digest}"


def _load_dir(path):
    if not path.is_dir():
        return []
    items = []
    for f in sorted(path.glob("*.json")):
        try:
            items.append((f, json.loads(f.read_text())))
        except json.JSONDecodeError as e:
            print(f"  ! skipping {f.relative_to(ROOT)}: invalid JSON ({e})")
    return items


def triage_comment(rec):
    lines = [f"**Triage by {AGENT}** · severity: {rec.get('severity') or 'none'} · confidence: {rec.get('confidence', '?')}"]
    if not rec.get("is_incident"):
        lines.append("Not a production incident — routed to the backlog. No page.")
    lines.append(f"**Root cause:** {rec.get('root_cause') or '—'}")
    if rec.get("evidence"):
        lines.append("**Evidence:**\n" + "\n".join(f"- {e}" for e in rec["evidence"]))
    if rec.get("runbook"):
        lines.append(f"**Runbook:** `{rec['runbook']}`")
    dep = rec.get("related_deploy")
    if dep:
        rollback = f" (rollback to {dep.get('rollback_to')})" if dep.get("rollback_to") else ""
        lines.append(f"**Related deploy:** {dep.get('service')} {dep.get('version')}{rollback}")
    if rec.get("linked_issues"):
        lines.append("**Linked:** " + ", ".join(rec["linked_issues"]))
    lines.append(f"**Recommended action:** {rec.get('recommended_action') or '—'}")
    if rec.get("page"):
        lines.append(f"**Page (needs human approval):** {rec['page']}")
    if rec.get("needs_human"):
        lines.append("⚠️ **Needs human review** — the agent was not confident enough to act on this alone.")
    return "\n\n".join(lines)


def write_triage(apply):
    summary = {"updated": [], "skipped": [], "missing_issue": []}
    for path, rec in _load_dir(ROOT / "out" / "triage"):
        issue_id = rec.get("id") or path.stem
        issue_path = ROOT / "issues" / f"{issue_id}.json"
        if not issue_path.exists():
            summary["missing_issue"].append(issue_id)
            continue
        issue = json.loads(issue_path.read_text())
        marker = _marker("triage", rec)
        if any(c.get("author") == AGENT and c.get("marker") == marker for c in issue.get("comments", [])):
            summary["skipped"].append(issue_id)
            continue
        labels = set(issue.get("labels") or [])
        labels.add("triaged")
        labels.add("incident" if rec.get("is_incident") else "enhancement")
        if rec.get("severity"):
            labels.add(f"sev:{rec['severity']}")
        if rec.get("needs_human"):
            labels.add("needs-human")
        issue["severity"] = rec.get("severity") if rec.get("is_incident") else None
        issue["labels"] = sorted(labels)
        issue.setdefault("comments", []).append(
            {"author": AGENT, "created_at": _now(), "marker": marker, "body": triage_comment(rec)})
        summary["updated"].append(issue_id)
        if apply:
            issue_path.write_text(json.dumps(issue, indent=2, ensure_ascii=False) + "\n")
    return summary


ANGLE_PLACEHOLDER = re.compile(r"<([A-Za-z_]+)>")
# <Vendor> / <rule> in a template mean the display forms, not the raw field values.
ANGLE_ALIASES = {"vendor": "vendor_title", "rule": "rule_title"}


def _to_format_template(template, field_names):
    """Accept <field> placeholders as well as {field}. Only names that are real fields are
    converted, so any other angle-bracket text in the template is left alone."""
    if not any(m.group(1).lower() in field_names or m.group(1).lower() in ANGLE_ALIASES
               for m in ANGLE_PLACEHOLDER.finditer(template)):
        return template  # already {field} style (e.g. the built-in template)
    escaped = template.replace("{", "{{").replace("}", "}}")

    def swap(m):
        name = m.group(1).lower()
        if name in ANGLE_ALIASES:
            return "{" + ANGLE_ALIASES[name] + "}"
        return "{" + name + "}" if name in field_names else m.group(0)

    return ANGLE_PLACEHOLDER.sub(swap, escaped)


def _render(finding, template):
    vendor = finding.get("vendor") or Path(finding.get("contract", "")).stem
    fields = _Blank({**{k: (v if v not in (None, "") else "—") for k, v in finding.items()},
                     "vendor_title": vendor, "rule_title": str(finding.get("rule", "")).replace("_", " ").title()})
    text = _to_format_template(template, set(fields)).format_map(fields)
    title, _, body = text.partition("\n")
    return title.strip().lstrip("# ").strip(), body.strip() + "\n"


def _github_titles():
    out = subprocess.run(["gh", "issue", "list", "--state", "all", "--limit", "500", "--json", "title"],
                         capture_output=True, text=True, check=True)
    return {i["title"] for i in json.loads(out.stdout)}


def write_compliance(apply, github):
    tpl_path = ROOT / "templates" / "violation_issue.md"
    template = tpl_path.read_text() if tpl_path.exists() else DEFAULT_TEMPLATE
    summary = {"opened": [], "skipped": [], "needs_review": [], "template": str(tpl_path.relative_to(ROOT)) if tpl_path.exists() else "built-in"}
    existing_titles = None
    if github and apply:
        if not shutil.which("gh"):
            raise SystemExit("--github needs the gh CLI")
        existing_titles = _github_titles()
    out_dir = ROOT / "compliance-issues"
    for path, finding in _load_dir(ROOT / "out" / "compliance"):
        status = finding.get("status")
        key = path.stem
        if status == "needs_review":
            summary["needs_review"].append(key)
            continue
        if status != "violation":
            continue
        title, body = _render(finding, template)
        local = out_dir / f"{key}.md"
        if local.exists() or (existing_titles is not None and title in existing_titles):
            summary["skipped"].append(key)
            continue
        summary["opened"].append({"key": key, "title": title})
        if apply:
            out_dir.mkdir(exist_ok=True)
            local.write_text(f"# {title}\n\n{body}")
            if github:
                subprocess.run(["gh", "issue", "create", "--title", title, "--body", body], check=True)
    return summary


def run(apply=False, github=False):
    return {"mode": "apply" if apply else "dry-run", "github": github,
            "triage": write_triage(apply), "compliance": write_compliance(apply, github)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--github", action="store_true", help="also open GitHub issues for violations (with --apply)")
    args = ap.parse_args()
    s = run(apply=args.apply, github=args.github)
    t, c = s["triage"], s["compliance"]
    print(f"writeback ({s['mode']}{', github' if s['github'] else ''})")
    print(f"  issues updated: {len(t['updated'])} {t['updated']}")
    print(f"  issues already up to date: {len(t['skipped'])} {t['skipped']}")
    if t["missing_issue"]:
        print(f"  ! triage records without an issue file: {t['missing_issue']}")
    print(f"  compliance issues opened: {len(c['opened'])}" + "".join(f"\n    - {o['title']}" for o in c["opened"]))
    print(f"  compliance issues already open: {len(c['skipped'])}")
    print(f"  needs human review (not opened): {len(c['needs_review'])} {c['needs_review']}")
    print(f"  template: {c['template']}")
    if not args.apply:
        print("  (dry run — nothing written; re-run with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
