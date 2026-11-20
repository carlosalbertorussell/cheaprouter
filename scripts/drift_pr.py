#!/usr/bin/env python3
"""
GitHub side of the SC7 daily drift check (.github/workflows/price-drift.yml).

Runs after scripts/drift_check.py and acts on its exit code, JSON and Markdown
through the GitHub CLI (`gh`) and git. It never merges, never enables
auto-merge, never approves, and never touches verified_at / verified_by.

    python scripts/drift_pr.py --exit-code N --json drift.json --md drift.md \
        --proposal prices.proposed.json [--base main] [--repo-dir .]

Behaviour by drift_check exit code:
  10  significant drift: find the open PR from `automation/price-drift`. If its
      body carries the same `<!-- sc7-fingerprint: ... -->`, do nothing.
      Otherwise reset that branch from the default branch, commit the proposal
      as prices.json, force-push the branch (automation-owned, nothing else is
      ever force-pushed), then open the PR or edit the existing one's body.
   0  no significant drift: if a drift PR is open, comment once that the drift
      has cleared (not again if the last comment already says so). The PR stays
      open for a human.
   2  invalid table or the feed failed everywhere: exit 1 with a message,
      nothing is opened.
 other (1, unexpected): exit 1, nothing is opened.

For exit 0 and 10, when the JSON says `expiry_warning`, one issue labelled
`price-expiry` titled "Price table expires on <expires_on>" is opened or
updated; open price-expiry issues for other dates are closed with a comment.

Untrusted text (the Markdown body) is always passed with --body-file, and every
command is an argv list (no shell). Tokens are read by gh from GH_TOKEN; this
script never reads or prints them.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

BRANCH = "automation/price-drift"
PR_LABELS = {
    "price-drift": ("d93f0b", "Proposed price change from the daily drift check"),
    "needs-human-verification": ("fbca04", "A human must verify every number on the provider's page"),
}
EXPIRY_LABEL = ("price-expiry", "b60205", "The price table is close to its max_age_days expiry")
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
FINGERPRINT_RE = re.compile(r"<!--\s*sc7-fingerprint:\s*([0-9a-fA-F]+)\s*-->")
CLEARED_MARKER = "<!-- sc7-drift-cleared -->"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXPIRY_TITLE = "Price table expires on {}"


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], Optional[str]], Result]


def subprocess_runner(cmd: Sequence[str], cwd: Optional[str] = None) -> Result:
    p = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)
    return Result(p.returncode, p.stdout, p.stderr)


class HelperError(Exception):
    pass


class Ctx:
    def __init__(self, runner: Runner, repo_dir: str, tmpdir: str):
        self.runner = runner
        self.repo_dir = repo_dir
        self.tmpdir = tmpdir
        self._n = 0

    def run(self, cmd: Sequence[str], check: bool = True) -> Result:
        res = self.runner(list(cmd), self.repo_dir)
        if check and res.returncode != 0:
            # Only the command name and stderr: gh/git never echo GH_TOKEN.
            raise HelperError(f"command failed ({res.returncode}): {' '.join(cmd[:3])} ...\n{res.stderr.strip()}")
        return res

    def run_json(self, cmd: Sequence[str]):
        out = self.run(cmd).stdout.strip()
        return json.loads(out) if out else []

    def body_file(self, text: str) -> str:
        self._n += 1
        path = Path(self.tmpdir) / f"body-{self._n}.md"
        path.write_text(text, encoding="utf-8")
        return str(path)


# ── helpers ──────────────────────────────────────────────────────────────────

def fingerprint_in(body: str | None) -> str | None:
    m = FINGERPRINT_RE.search(body or "")
    return m.group(1).lower() if m else None


def open_drift_pr(ctx: Ctx) -> dict | None:
    prs = ctx.run_json(["gh", "pr", "list", "--head", BRANCH, "--state", "open",
                        "--json", "number,body,url", "--limit", "1"])
    return prs[0] if prs else None


def ensure_labels(ctx: Ctx, labels: dict[str, tuple[str, str]]) -> None:
    existing = {l["name"] for l in ctx.run_json(["gh", "label", "list", "--json", "name", "--limit", "500"])}
    for name, (color, desc) in labels.items():
        if name not in existing:
            ctx.run(["gh", "label", "create", name, "--color", color, "--description", desc])


def default_branch(ctx: Ctx) -> str:
    out = ctx.run(["gh", "repo", "view", "--json", "defaultBranchRef",
                   "--jq", ".defaultBranchRef.name"]).stdout.strip()
    if not out:
        raise HelperError("could not determine the default branch")
    return out


def check_proposal(proposal: dict, current: dict) -> None:
    """Defence in depth for the provenance rules (the engine already does this)."""
    for key in ("verified_at", "verified_by"):
        if proposal.get(key) != current.get(key):
            raise HelperError(f"proposal changes {key}; automation must never touch it")
    if "_candidate" in proposal:
        raise HelperError("proposal carries a _candidate key; refusing to commit it")


def pr_body(md: str, fingerprint: str) -> str:
    if fingerprint_in(md) != fingerprint:
        md = md.rstrip("\n") + f"\n\n<!-- sc7-fingerprint: {fingerprint} -->\n"
    return md


# ── exit 10 ──────────────────────────────────────────────────────────────────

def handle_drift(ctx: Ctx, report: dict, md: str, proposal_path: Path, base: str | None) -> None:
    fingerprint = str(report.get("fingerprint") or "").lower()
    if not re.fullmatch(r"[0-9a-f]+", fingerprint):
        raise HelperError("drift JSON has no valid fingerprint")
    if not proposal_path.is_file():
        raise HelperError(f"exit 10 but no proposal at {proposal_path}")

    pr = open_drift_pr(ctx)
    if pr and fingerprint_in(pr.get("body")) == fingerprint:
        print(f"Drift PR #{pr['number']} already carries fingerprint {fingerprint[:12]}; nothing to do.")
        return

    proposal_text = proposal_path.read_text(encoding="utf-8")
    proposal = json.loads(proposal_text)
    base = base or default_branch(ctx)
    n = len(report.get("significant") or [])

    # Reset the automation-owned branch from the default branch.
    ctx.run(["git", "fetch", "--no-tags", "origin", base])
    ctx.run(["git", "checkout", "-B", BRANCH, f"origin/{base}"])
    prices = Path(ctx.repo_dir) / "prices.json"
    check_proposal(proposal, json.loads(prices.read_text(encoding="utf-8")))
    shutil.copyfile(proposal_path, prices)
    if ctx.run(["git", "diff", "--quiet", "--", "prices.json"], check=False).returncode == 0:
        print(f"Proposal is identical to {base}'s prices.json; nothing to propose.")
        return
    ctx.run(["git", "config", "user.name", BOT_NAME])
    ctx.run(["git", "config", "user.email", BOT_EMAIL])
    ctx.run(["git", "add", "prices.json"])
    msg = (
        f"chore(prices): propose {n} price change(s) from the daily drift check\n\n"
        "Numbers come from the OpenRouter feed (proxy provenance) and are NOT verified.\n"
        "verified_at and verified_by are unchanged; a human must verify each number\n"
        "on the provider's pricing page and update them before merging.\n\n"
        f"sc7-fingerprint: {fingerprint}\n"
    )
    ctx.run(["git", "commit", "-F", ctx.body_file(msg)])
    # Force-push ONLY the automation-owned branch.
    ctx.run(["git", "push", "--force", "origin", f"HEAD:refs/heads/{BRANCH}"])

    ensure_labels(ctx, PR_LABELS)
    title = f"Price drift: {n} significant change(s) need human verification"
    body = ctx.body_file(pr_body(md, fingerprint))
    if pr:
        ctx.run(["gh", "pr", "edit", str(pr["number"]), "--title", title, "--body-file", body,
                 *[a for l in PR_LABELS for a in ("--add-label", l)]])
        print(f"Updated drift PR #{pr['number']} (fingerprint {fingerprint[:12]}).")
    else:
        ctx.run(["gh", "pr", "create", "--base", base, "--head", BRANCH, "--title", title,
                 "--body-file", body, *[a for l in PR_LABELS for a in ("--label", l)]])
        print(f"Opened drift PR (fingerprint {fingerprint[:12]}).")


# ── exit 0 ───────────────────────────────────────────────────────────────────

def handle_clear(ctx: Ctx, report: dict) -> None:
    pr = open_drift_pr(ctx)
    if not pr:
        print("No significant drift and no open drift PR.")
        return
    view = ctx.run_json(["gh", "pr", "view", str(pr["number"]), "--json", "comments"])
    comments = (view or {}).get("comments") or []
    if comments and CLEARED_MARKER in (comments[-1].get("body") or ""):
        print(f"Drift PR #{pr['number']} already has a 'drift cleared' comment.")
        return
    text = (
        f"{CLEARED_MARKER}\n"
        f"The daily drift check at {report.get('checked_at', 'the latest run')} found no "
        "significant drift: the feed now matches prices.json within the threshold. "
        "This PR is left open for a human to close or merge."
    )
    ctx.run(["gh", "pr", "comment", str(pr["number"]), "--body-file", ctx.body_file(text)])
    print(f"Commented on drift PR #{pr['number']} that drift has cleared.")


# ── expiry ───────────────────────────────────────────────────────────────────

def handle_expiry(ctx: Ctx, report: dict) -> None:
    expires_on = str(report.get("expires_on") or "")
    if not DATE_RE.match(expires_on):
        print("No valid expires_on in the drift JSON; skipping expiry handling.")
        return
    title = EXPIRY_TITLE.format(expires_on)
    issues = ctx.run_json(["gh", "issue", "list", "--label", EXPIRY_LABEL[0], "--state", "open",
                           "--json", "number,title,body", "--limit", "100"])
    for issue in issues:
        if issue.get("title") != title:
            ctx.run(["gh", "issue", "close", str(issue["number"]), "--comment",
                     f"Superseded: the price table now expires on {expires_on}."])
            print(f"Closed stale price-expiry issue #{issue['number']}.")
    if not report.get("expiry_warning"):
        return
    body = (
        f"The price table (verified_at {report.get('verified_at')}, max_age_days "
        f"{report.get('max_age_days')}) expires on **{expires_on}** "
        f"({report.get('days_to_expiry')} day(s) left). After that, routing refuses with "
        "`price_table_stale`.\n\nRe-verify every price on the provider pages listed in "
        "prices.json `pricing_urls`, then update `verified_at` and `verified_by`.\n"
    )
    current = [i for i in issues if i.get("title") == title]
    if current:
        if (current[0].get("body") or "").strip() != body.strip():
            ctx.run(["gh", "issue", "edit", str(current[0]["number"]), "--body-file", ctx.body_file(body)])
            print(f"Updated price-expiry issue #{current[0]['number']}.")
        return
    ensure_labels(ctx, {EXPIRY_LABEL[0]: EXPIRY_LABEL[1:]})
    ctx.run(["gh", "issue", "create", "--title", title, "--body-file", ctx.body_file(body),
             "--label", EXPIRY_LABEL[0]])
    print(f"Opened issue: {title}.")


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--exit-code", type=int, required=True, help="drift_check.py's exit code")
    ap.add_argument("--json", required=True, help="drift_check.py --json-out file")
    ap.add_argument("--md", required=True, help="drift_check.py --md-out file")
    ap.add_argument("--proposal", required=True, help="drift_check.py --write-proposal file")
    ap.add_argument("--base", default=None, help="default branch (queried with gh if omitted)")
    ap.add_argument("--repo-dir", default=".", help="repository checkout (default: .)")
    args = ap.parse_args(argv)

    code = args.exit_code
    if code == 2:
        print("::error::drift check exit 2: the price table is invalid or the feed failed for every "
              "provider. No PR or issue was opened.")
        return 1
    if code not in (0, 10):
        print(f"::error::drift check failed unexpectedly (exit {code}). No PR or issue was opened.")
        return 1

    try:
        report = json.loads(Path(args.json).read_text(encoding="utf-8"))
        md = Path(args.md).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read drift check output: {exc}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        ctx = Ctx(runner or subprocess_runner, args.repo_dir, tmp)
        try:
            if code == 10:
                handle_drift(ctx, report, md, Path(args.proposal), args.base)
            else:
                handle_clear(ctx, report)
            handle_expiry(ctx, report)
        except HelperError as exc:
            print(f"::error::{exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
