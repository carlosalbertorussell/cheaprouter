"""SC7 workflow helper (scripts/drift_pr.py) and .github/workflows/price-drift.yml.

A fake runner records every gh/git command and answers the read-only ones, so no
test touches the network or a real repository.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import drift_pr  # noqa: E402

FP_OLD = "a" * 64
FP_NEW = "b" * 64

# Commands that only read state. Anything else counts as a change.
READ_ONLY = {("gh", "pr", "list"), ("gh", "pr", "view"), ("gh", "issue", "list"),
             ("gh", "label", "list"), ("gh", "repo", "view"), ("git", "diff", "--quiet")}


class FakeRunner:
    def __init__(self, prs=None, comments=None, issues=None, labels=None, diff_rc=1):
        self.calls: list[list[str]] = []
        self.files: dict[int, str] = {}  # call index -> --body-file / -F content
        self.prs = prs or []
        self.comments = comments or []
        self.issues = issues or []
        self.labels = labels or []
        self.diff_rc = diff_rc

    def __call__(self, cmd, cwd=None):
        cmd = list(cmd)
        assert all(isinstance(a, str) for a in cmd)
        self.calls.append(cmd)
        for flag in ("--body-file", "-F"):
            if flag in cmd:
                self.files[len(self.calls) - 1] = Path(cmd[cmd.index(flag) + 1]).read_text()
        head = tuple(cmd[:3])
        if head == ("gh", "pr", "list"):
            return drift_pr.Result(0, json.dumps(self.prs))
        if head == ("gh", "pr", "view"):
            return drift_pr.Result(0, json.dumps({"comments": self.comments}))
        if head == ("gh", "issue", "list"):
            return drift_pr.Result(0, json.dumps(self.issues))
        if head == ("gh", "label", "list"):
            return drift_pr.Result(0, json.dumps([{"name": n} for n in self.labels]))
        if head == ("gh", "repo", "view"):
            return drift_pr.Result(0, "main\n")
        if cmd[:2] == ["git", "diff"]:
            return drift_pr.Result(self.diff_rc)
        return drift_pr.Result(0)

    def mutating(self):
        return [c for c in self.calls if tuple(c[:3]) not in READ_ONLY]

    def find(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def env(tmp_path):
    """A fake checkout with prices.json and drift_check outputs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    table = json.loads((REPO / "prices.json").read_text())
    (repo / "prices.json").write_text(json.dumps(table, indent=2) + "\n")
    proposal = json.loads(json.dumps(table))
    proposal["notes"] = "changed by the proposal"
    out = tmp_path / "out"
    out.mkdir()
    (out / "proposal.json").write_text(json.dumps(proposal, indent=2) + "\n")

    def run(exit_code, runner, fingerprint=FP_NEW, expiry_warning=False,
            expires_on="2026-12-29", md=None, **extra):
        report = {
            "checked_at": "2026-11-20T05:37:00Z", "verified_at": "2026-11-14",
            "max_age_days": 45, "expires_on": expires_on, "days_to_expiry": 9,
            "expiry_warning": expiry_warning, "threshold_pct": 5.0,
            "significant": [{"provider": "openai", "tier": "tier_fast", "model_id": "gpt-6-luna",
                             "field": "input_price_per_1m", "table": 0.10, "feed": 0.12,
                             "pct": 20.0}] if exit_code == 10 else [],
            "minor": [], "fetch_failed": [], "manual": ["groq", "qwen"],
            "fingerprint": fingerprint,
        }
        report.update(extra)
        (out / "drift.json").write_text(json.dumps(report))
        body = md if md is not None else (
            "## Price drift\n\n| openai | `$(rm -rf /)` \"quoted\" |\n\n"
            f"<!-- sc7-fingerprint: {fingerprint} -->\n")
        (out / "drift.md").write_text(body)
        return drift_pr.main([
            "--exit-code", str(exit_code), "--json", str(out / "drift.json"),
            "--md", str(out / "drift.md"), "--proposal", str(out / "proposal.json"),
            "--repo-dir", str(repo), "--base", "main",
        ], runner=runner)

    run.repo = repo
    run.proposal = out / "proposal.json"
    return run


def _no_merge(runner):
    for c in runner.calls:
        joined = " ".join(c)
        assert "merge" not in c and "--auto" not in c and "approve" not in c, joined
        assert not (c[:3] == ["gh", "pr", "review"]), joined


# ── exit 10 ──────────────────────────────────────────────────────────────────

def test_drift_no_open_pr_creates_branch_commit_push_and_pr(env):
    r = FakeRunner()
    assert env(10, r) == 0
    m = r.mutating()
    kinds = [c[:2] if c[0] == "git" else c[:3] for c in m]
    assert kinds == [
        ["git", "fetch"], ["git", "checkout"], ["git", "config"], ["git", "config"],
        ["git", "add"], ["git", "commit"], ["git", "push"],
        ["gh", "label", "create"], ["gh", "label", "create"], ["gh", "pr", "create"],
    ]
    assert ["git", "checkout", "-B", "automation/price-drift", "origin/main"] in m
    assert ["git", "config", "user.name", "github-actions[bot]"] in m
    assert ["git", "push", "--force", "origin", "HEAD:refs/heads/automation/price-drift"] in m
    assert (env.repo / "prices.json").read_text() == env.proposal.read_text()
    create = r.find("gh", "pr", "create")[0]
    assert create[create.index("--head") + 1] == "automation/price-drift"
    assert create[create.index("--base") + 1] == "main"
    labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
    assert labels == ["price-drift", "needs-human-verification"]
    assert {c[3] for c in r.find("gh", "label", "create")} == {"price-drift", "needs-human-verification"}
    # The only push is to the automation branch.
    assert all("automation/price-drift" in " ".join(c) for c in r.find("git", "push"))
    _no_merge(r)


def test_drift_existing_labels_are_not_recreated(env):
    r = FakeRunner(labels=["price-drift", "needs-human-verification"])
    assert env(10, r) == 0
    assert r.find("gh", "label", "create") == []
    assert len(r.find("gh", "pr", "create")) == 1


def test_drift_same_fingerprint_changes_nothing(env):
    r = FakeRunner(prs=[{"number": 7, "url": "u", "body": f"x\n<!-- sc7-fingerprint: {FP_NEW} -->\n"}])
    before = (env.repo / "prices.json").read_text()
    assert env(10, r) == 0
    assert r.mutating() == []
    assert not any(c[0] == "git" for c in r.calls)
    assert (env.repo / "prices.json").read_text() == before


def test_drift_new_fingerprint_updates_branch_and_edits_body(env):
    r = FakeRunner(prs=[{"number": 7, "url": "u", "body": f"<!-- sc7-fingerprint: {FP_OLD} -->"}],
                   labels=["price-drift", "needs-human-verification"])
    assert env(10, r) == 0
    assert len(r.find("git", "push")) == 1
    assert r.find("gh", "pr", "create") == []
    edits = r.find("gh", "pr", "edit")
    assert len(edits) == 1 and edits[0][3] == "7"
    idx = r.calls.index(edits[0])
    assert f"<!-- sc7-fingerprint: {FP_NEW} -->" in r.files[idx]
    _no_merge(r)


def test_body_is_passed_by_file_never_inline(env):
    r = FakeRunner()
    assert env(10, r) == 0
    create = r.find("gh", "pr", "create")[0]
    assert "--body" not in create and "--body-file" in create
    body = r.files[r.calls.index(create)]
    assert "`$(rm -rf /)` \"quoted\"" in body
    assert not any("rm -rf" in a for c in r.calls for a in c)


def test_body_gets_fingerprint_marker_if_markdown_lacks_it(env):
    r = FakeRunner()
    assert env(10, r, md="## Drift without a marker\n") == 0
    body = r.files[r.calls.index(r.find("gh", "pr", "create")[0])]
    assert drift_pr.fingerprint_in(body) == FP_NEW


def test_commit_message_by_file_and_says_unverified(env):
    r = FakeRunner()
    assert env(10, r) == 0
    commit = r.find("git", "commit")[0]
    msg = r.files[r.calls.index(commit)]
    assert "NOT verified" in msg and FP_NEW in msg


def test_drift_refuses_proposal_that_touches_verified_at(env):
    p = json.loads(env.proposal.read_text())
    p["verified_at"] = "2099-01-01"
    env.proposal.write_text(json.dumps(p))
    r = FakeRunner()
    assert env(10, r) == 1
    assert r.find("git", "commit") == [] and r.find("git", "push") == []
    assert r.find("gh", "pr", "create") == []


def test_drift_missing_proposal_fails(env):
    env.proposal.unlink()
    r = FakeRunner()
    assert env(10, r) == 1
    assert r.mutating() == []


def test_drift_identical_proposal_pushes_nothing(env):
    r = FakeRunner(diff_rc=0)
    assert env(10, r) == 0
    assert r.find("git", "commit") == [] and r.find("git", "push") == []
    assert r.find("gh", "pr", "create") == []


def test_drift_without_base_queries_default_branch(env):
    env(10, FakeRunner())  # writes the drift outputs
    out = env.repo.parent / "out"
    r = FakeRunner()
    assert drift_pr.main(["--exit-code", "10", "--json", str(out / "drift.json"),
                          "--md", str(out / "drift.md"), "--proposal", str(env.proposal),
                          "--repo-dir", str(env.repo)], runner=r) == 0
    assert r.find("gh", "repo", "view")
    assert ["git", "checkout", "-B", "automation/price-drift", "origin/main"] in r.calls


def test_drift_bad_fingerprint_fails(env):
    r = FakeRunner()
    assert env(10, r, fingerprint="") == 1
    assert r.mutating() == []


# ── exit 0 ───────────────────────────────────────────────────────────────────

def test_clear_comments_once_on_open_pr(env):
    r = FakeRunner(prs=[{"number": 7, "url": "u", "body": ""}])
    assert env(0, r) == 0
    comments = r.find("gh", "pr", "comment")
    assert len(comments) == 1 and comments[0][3] == "7" and "--body-file" in comments[0]
    text = r.files[r.calls.index(comments[0])]
    assert drift_pr.CLEARED_MARKER in text
    assert r.find("gh", "pr", "close") == []
    _no_merge(r)

    # Next run: the last comment already says so -> no second comment.
    r2 = FakeRunner(prs=[{"number": 7, "url": "u", "body": ""}],
                    comments=[{"body": "human note"}, {"body": text}])
    assert env(0, r2) == 0
    assert r2.find("gh", "pr", "comment") == []
    assert r2.mutating() == []


def test_clear_comments_again_if_a_later_comment_followed(env):
    r = FakeRunner(prs=[{"number": 7, "url": "u", "body": ""}],
                   comments=[{"body": drift_pr.CLEARED_MARKER}, {"body": "human: reopened drift?"}])
    assert env(0, r) == 0
    assert len(r.find("gh", "pr", "comment")) == 1


def test_clear_without_open_pr_does_nothing(env):
    r = FakeRunner()
    assert env(0, r) == 0
    assert r.mutating() == []
    assert not any(c[0] == "git" for c in r.calls)


# ── exit 2 / other ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [2, 1, 3])
def test_failure_exit_codes_fail_and_create_nothing(env, code, capsys):
    r = FakeRunner(prs=[{"number": 7, "url": "u", "body": ""}])
    assert env(code, r, expiry_warning=True) == 1
    assert r.calls == []
    out = capsys.readouterr().out
    assert "::error::" in out and "No PR or issue was opened" in out


def test_unreadable_json_fails(env, tmp_path):
    r = FakeRunner()
    assert drift_pr.main(["--exit-code", "0", "--json", str(tmp_path / "missing.json"),
                          "--md", str(tmp_path / "missing.md"), "--proposal", "x"], runner=r) == 1
    assert r.calls == []


def test_command_failure_is_reported_without_crash(env, capsys):
    class Failing(FakeRunner):
        def __call__(self, cmd, cwd=None):
            res = super().__call__(cmd, cwd)
            if cmd[:3] == ["gh", "pr", "create"]:
                return drift_pr.Result(1, "", "boom")
            return res
    r = Failing()
    assert env(10, r) == 1
    assert "boom" in capsys.readouterr().out


# ── expiry issue ─────────────────────────────────────────────────────────────

def test_expiry_issue_created_with_label(env):
    r = FakeRunner()
    assert env(0, r, expiry_warning=True) == 0
    create = r.find("gh", "issue", "create")
    assert len(create) == 1
    c = create[0]
    assert c[c.index("--title") + 1] == "Price table expires on 2026-12-29"
    assert c[c.index("--label") + 1] == "price-expiry" and "--body-file" in c
    assert [x[3] for x in r.find("gh", "label", "create")] == ["price-expiry"]


def test_expiry_issue_deduped_by_title(env):
    r = FakeRunner(labels=["price-expiry"])
    assert env(0, r, expiry_warning=True) == 0
    body = r.files[r.calls.index(r.find("gh", "issue", "create")[0])]
    issue = {"number": 3, "title": "Price table expires on 2026-12-29", "body": body}

    same = FakeRunner(issues=[issue])
    assert env(0, same, expiry_warning=True) == 0
    assert same.mutating() == []

    changed = FakeRunner(issues=[dict(issue, body="old text")])
    assert env(0, changed, expiry_warning=True) == 0
    assert changed.find("gh", "issue", "create") == []
    edits = changed.find("gh", "issue", "edit")
    assert len(edits) == 1 and edits[0][3] == "3" and "--body-file" in edits[0]


def test_expiry_closes_older_issues(env):
    r = FakeRunner(labels=["price-expiry"], issues=[
        {"number": 1, "title": "Price table expires on 2026-10-01", "body": ""},
        {"number": 2, "title": "Price table expires on 2026-12-29", "body": ""},
    ])
    assert env(0, r, expiry_warning=True) == 0
    closes = r.find("gh", "issue", "close")
    assert len(closes) == 1 and closes[0][3] == "1" and "--comment" in closes[0]
    assert r.find("gh", "issue", "create") == []
    assert [c[3] for c in r.find("gh", "issue", "edit")] == ["2"]


def test_expiry_cleared_after_reverification_closes_old_issue(env):
    r = FakeRunner(issues=[{"number": 1, "title": "Price table expires on 2026-12-29", "body": ""}])
    assert env(0, r, expiry_warning=False, expires_on="2027-02-10") == 0
    assert [c[3] for c in r.find("gh", "issue", "close")] == ["1"]
    assert r.find("gh", "issue", "create") == []


def test_expiry_no_warning_no_issues_changes_nothing(env):
    r = FakeRunner()
    assert env(0, r) == 0
    assert r.mutating() == []


def test_expiry_invalid_date_is_ignored(env):
    r = FakeRunner()
    assert env(0, r, expiry_warning=True, expires_on="soon; rm -rf /") == 0
    assert r.mutating() == []


def test_expiry_handled_alongside_drift(env):
    r = FakeRunner()
    assert env(10, r, expiry_warning=True) == 0
    assert len(r.find("gh", "pr", "create")) == 1
    assert len(r.find("gh", "issue", "create")) == 1
    _no_merge(r)


def test_default_runner_is_subprocess(monkeypatch):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return P()

    monkeypatch.setattr(drift_pr.subprocess, "run", fake_run)
    res = drift_pr.subprocess_runner(["gh", "--version"], "/x")
    assert res.stdout == "ok" and seen["cmd"] == ["gh", "--version"]
    assert seen["kw"]["cwd"] == "/x" and "shell" not in seen["kw"]


def test_helper_source_has_no_merge_or_shell():
    src = (REPO / "scripts" / "drift_pr.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    for bad in ('"merge"', "--auto", "approve", "shell=True", "os.system"):
        assert bad not in code, bad


# ── workflow file ────────────────────────────────────────────────────────────

WF = REPO / ".github" / "workflows" / "price-drift.yml"


def _load_workflow():
    text = WF.read_text()
    try:
        import yaml
    except ImportError:  # CI installs no PyYAML; fall back to text checks
        return text, None
    return text, yaml.safe_load(text)


def test_workflow_schedule_permissions_and_steps():
    text, wf = _load_workflow()
    cron = re.search(r'cron:\s*"([^"]+)"', text).group(1)
    minute, hour, dom, mon, dow = cron.split()
    assert minute not in ("0", "00", "*") and minute.isdigit()
    assert (dom, mon, dow) == ("*", "*", "*")  # daily
    assert "workflow_dispatch" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert re.search(r"python scripts/drift_check\.py[\s\\]+--json-out", text)
    assert "--md-out" in text and "--write-proposal" in text
    assert "python scripts/drift_pr.py" in text
    assert "|| code=$?" in text  # exit 10 does not fail the step
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses and set(uses) <= {"actions/checkout@v7", "actions/setup-python@v7"}
    assert not re.search(r"gh pr (merge|review)|--auto\b", text)

    if wf is not None:
        on = wf.get("on", wf.get(True))  # PyYAML parses bare `on` as True
        assert on["schedule"][0]["cron"] == cron
        assert "workflow_dispatch" in on
        assert wf["permissions"] == {"contents": "write", "pull-requests": "write", "issues": "write"}
        assert wf["concurrency"]["group"] and wf["concurrency"]["cancel-in-progress"] is False
        steps = wf["jobs"]["drift"]["steps"]
        assert any(s.get("with", {}).get("python-version") == "3.12" for s in steps)
        runs = [s.get("run", "") for s in steps]
        assert any("pip install -r requirements.txt" in r for r in runs)
        pr_step = next(s for s in steps if "drift_pr.py" in s.get("run", ""))
        assert pr_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    else:
        assert re.search(r"^permissions:\n  contents: write\s*(#.*)?\n  pull-requests: write\s*(#.*)?\n"
                         r"  issues: write", text, re.M)
        assert re.search(r"^concurrency:\n  group: \S+", text, re.M)
        assert 'python-version: "3.12"' in text


def test_workflow_has_no_untrusted_expression_in_run():
    text = WF.read_text()
    # Every ${{ }} used by a shell must come through env:, not inline in run:.
    in_run = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("run:"):
            in_run = True
            indent = len(line) - len(line.lstrip())
            continue
        if in_run and stripped and (len(line) - len(line.lstrip())) <= indent:
            in_run = False
        if in_run:
            assert "${{" not in line, line
