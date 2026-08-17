"""
Forge API clients (GitHub / GitLab / Gitea) for fleet-memory miner.

Each client implements: list_repos(), repo_fingerprint(), fetch_issues(), fetch_commits().
The miner calls process_forge_repo() which is forge-agnostic.

Credentials:
  GitHub  — GITHUB_TOKEN env var; optional GITHUB_URL for Enterprise
  GitLab  — GITLAB_TOKEN env var; optional GITLAB_URL for self-hosted
  Gitea   — GITEA_TOKEN env var, or git credential store fallback
"""

import subprocess
import urllib.parse
from abc import ABC, abstractmethod

import httpx

MAX_COMMITS = 200

# Commit subjects to skip (noisy/auto-generated)
GIT_SKIP_SUBJECTS = {"merge branch", "merge pull request", "initial commit", "wip", "update readme"}


class ForgeClient(ABC):
    prefix: str  # checkpoint key prefix: "github" | "gitlab" | "gitea"

    @abstractmethod
    def list_repos(self) -> list[tuple[str, str]]:
        """Return (owner, repo) pairs."""

    @abstractmethod
    def repo_fingerprint(self, owner: str, repo: str) -> str:
        """Return short string that changes when repo content changes."""

    @abstractmethod
    def fetch_issues(self, owner: str, repo: str) -> str:
        """Return formatted issue + comment text block."""

    @abstractmethod
    def fetch_commits(self, owner: str, repo: str) -> str:
        """Return formatted commit log text block."""


class GitHubForgeClient(ForgeClient):
    prefix = "github"

    def __init__(self, token: str, base_url: str = "https://api.github.com", orgs: list[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.orgs = orgs or []
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return httpx.get(f"{self.base_url}{path}", params=params, headers=self.headers, timeout=30)

    def list_repos(self) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        if self.orgs:
            for org in self.orgs:
                page = 1
                while True:
                    resp = self._get(f"/orgs/{org}/repos", {"type": "all", "per_page": 100, "page": page})
                    if resp.status_code != 200:
                        break
                    repos = resp.json()
                    if not repos:
                        break
                    for r in repos:
                        results.append((r["owner"]["login"], r["name"]))
                    if len(repos) < 100:
                        break
                    page += 1
        else:
            page = 1
            while True:
                resp = self._get("/user/repos", {"affiliation": "owner", "per_page": 100, "page": page})
                if resp.status_code != 200:
                    break
                repos = resp.json()
                if not repos:
                    break
                for r in repos:
                    results.append((r["owner"]["login"], r["name"]))
                if len(repos) < 100:
                    break
                page += 1
        return results

    def repo_fingerprint(self, owner: str, repo: str) -> str:
        try:
            resp = self._get(f"/repos/{owner}/{repo}")
            if resp.status_code == 200:
                return resp.json().get("updated_at", "")[:16]
        except Exception:
            pass
        return ""

    def fetch_issues(self, owner: str, repo: str) -> str:
        parts: list[str] = []
        page = 1
        while True:
            resp = self._get(f"/repos/{owner}/{repo}/issues", {"state": "all", "per_page": 100, "page": page})
            if resp.status_code != 200:
                break
            issues = resp.json()
            if not issues:
                break
            for issue in issues:
                if "pull_request" in issue:  # GitHub returns PRs here — skip them
                    continue
                num = issue["number"]
                state = issue["state"]
                title = issue["title"]
                body = (issue.get("body") or "").strip()[:1500]
                labels = ", ".join(lb["name"] for lb in issue.get("labels", []))
                header = f"ISSUE #{num} [{state}]: {title}"
                if labels:
                    header += f"  [labels: {labels}]"
                parts.append(header)
                if body:
                    parts.append(f"Body: {body}")
                if issue.get("comments", 0) > 0:
                    try:
                        cr = self._get(f"/repos/{owner}/{repo}/issues/{num}/comments")
                        if cr.status_code == 200:
                            for c in cr.json():
                                cbody = (c.get("body") or "").strip()[:800]
                                if cbody:
                                    parts.append(f"  COMMENT: {cbody}")
                    except Exception:
                        pass
                parts.append("")
            if len(issues) < 100:
                break
            page += 1
        return "\n".join(parts).strip()

    def fetch_commits(self, owner: str, repo: str) -> str:
        parts: list[str] = []
        page = 1
        fetched = 0
        while fetched < MAX_COMMITS:
            resp = self._get(f"/repos/{owner}/{repo}/commits", {"per_page": 100, "page": page})
            if resp.status_code != 200:
                break
            commits = resp.json()
            if not commits:
                break
            for c in commits:
                sha = c.get("sha", "")[:8]
                commit = c.get("commit", {})
                msg = (commit.get("message") or "").strip()
                author = commit.get("author", {}).get("name", "")
                date = (commit.get("author", {}).get("date") or "")[:10]
                if not msg or len(msg) < 10:
                    continue
                if any(msg.split("\n")[0].lower().startswith(p) for p in GIT_SKIP_SUBJECTS):
                    continue
                parts.append(f"COMMIT {sha} by {author} on {date}:\n{msg[:600]}")
                parts.append("")
            fetched += len(commits)
            if len(commits) < 100:
                break
            page += 1
        return "\n".join(parts).strip()


class GitLabForgeClient(ForgeClient):
    prefix = "gitlab"

    def __init__(self, token: str, base_url: str = "https://gitlab.com", groups: list[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.groups = groups or []
        self.headers = {"PRIVATE-TOKEN": token}

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return httpx.get(f"{self.base_url}/api/v4{path}", params=params, headers=self.headers, timeout=30)

    def _encode(self, owner: str, repo: str) -> str:
        return urllib.parse.quote(f"{owner}/{repo}", safe="")

    def list_repos(self) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        if self.groups:
            for group in self.groups:
                page = 1
                while True:
                    resp = self._get(f"/groups/{group}/projects",
                        {"include_subgroups": "true", "simple": "true", "per_page": 100, "page": page})
                    if resp.status_code != 200:
                        break
                    projects = resp.json()
                    if not projects:
                        break
                    for p in projects:
                        ns = p.get("namespace", {}).get("full_path", "")
                        name = p.get("path", "")
                        if ns and name:
                            results.append((ns, name))
                    if len(projects) < 100:
                        break
                    page += 1
        else:
            page = 1
            while True:
                resp = self._get("/projects", {"owned": "true", "simple": "true", "per_page": 100, "page": page})
                if resp.status_code != 200:
                    break
                projects = resp.json()
                if not projects:
                    break
                for p in projects:
                    ns = p.get("namespace", {}).get("full_path", "")
                    name = p.get("path", "")
                    if ns and name:
                        results.append((ns, name))
                if len(projects) < 100:
                    break
                page += 1
        return results

    def repo_fingerprint(self, owner: str, repo: str) -> str:
        try:
            resp = self._get(f"/projects/{self._encode(owner, repo)}")
            if resp.status_code == 200:
                return resp.json().get("last_activity_at", "")[:16]
        except Exception:
            pass
        return ""

    def fetch_issues(self, owner: str, repo: str) -> str:
        encoded = self._encode(owner, repo)
        parts: list[str] = []
        for state in ["opened", "closed"]:  # GitLab uses "opened", not "open"
            page = 1
            while True:
                resp = self._get(f"/projects/{encoded}/issues", {"state": state, "per_page": 100, "page": page})
                if resp.status_code != 200:
                    break
                issues = resp.json()
                if not issues:
                    break
                for issue in issues:
                    iid = issue["iid"]  # per-project ID — required for notes endpoint (not global id)
                    title = issue["title"]
                    body = (issue.get("description") or "").strip()[:1500]
                    raw_labels = issue.get("labels", [])
                    labels = ", ".join(lb if isinstance(lb, str) else lb.get("name", "") for lb in raw_labels)
                    header = f"ISSUE #{iid} [{state}]: {title}"
                    if labels:
                        header += f"  [labels: {labels}]"
                    parts.append(header)
                    if body:
                        parts.append(f"Body: {body}")
                    if issue.get("user_notes_count", 0) > 0:
                        try:
                            nr = self._get(f"/projects/{encoded}/issues/{iid}/notes", {"per_page": 50})
                            if nr.status_code == 200:
                                for note in nr.json():
                                    if note.get("system"):  # skip system notes (label changes, assignments)
                                        continue
                                    nbody = (note.get("body") or "").strip()[:800]
                                    if nbody:
                                        parts.append(f"  COMMENT: {nbody}")
                        except Exception:
                            pass
                    parts.append("")
                if len(issues) < 100:
                    break
                page += 1
        return "\n".join(parts).strip()

    def fetch_commits(self, owner: str, repo: str) -> str:
        encoded = self._encode(owner, repo)
        parts: list[str] = []
        page = 1
        fetched = 0
        while fetched < MAX_COMMITS:
            resp = self._get(f"/projects/{encoded}/repository/commits", {"per_page": 100, "page": page})
            if resp.status_code != 200:
                break
            commits = resp.json()
            if not commits:
                break
            for c in commits:
                sha = c.get("id", "")[:8]
                msg = (c.get("message") or "").strip()
                author = c.get("author_name", "")
                date = (c.get("authored_date") or "")[:10]
                if not msg or len(msg) < 10:
                    continue
                if any(msg.split("\n")[0].lower().startswith(p) for p in GIT_SKIP_SUBJECTS):
                    continue
                parts.append(f"COMMIT {sha} by {author} on {date}:\n{msg[:600]}")
                parts.append("")
            fetched += len(commits)
            if len(commits) < 100:
                break
            page += 1
        return "\n".join(parts).strip()


class GiteaForgeClient(ForgeClient):
    prefix = "gitea"

    def __init__(self, base_url: str, orgs: list[str], token: str = "", user: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.orgs = orgs
        self._auth = (user, password) if user and not token else None
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return httpx.get(f"{self.base_url}{path}", params=params,
                         auth=self._auth, headers=self._headers, timeout=30)

    def list_repos(self) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        if self.orgs:
            targets = [(f"/api/v1/orgs/{org}/repos", org) for org in self.orgs]
        else:
            targets = [("/api/v1/repos/search", None)]
        for path, org_label in targets:
            page = 1
            while True:
                resp = self._get(path, {"limit": 50, "page": page})
                if resp.status_code != 200:
                    break
                data = resp.json()
                repos = data.get("data", data) if isinstance(data, dict) else data
                if not repos:
                    break
                for r in repos:
                    owner = r.get("owner", {}).get("login", org_label or "")
                    results.append((owner, r["name"]))
                if len(repos) < 50:
                    break
                page += 1
        return results

    def repo_fingerprint(self, owner: str, repo: str) -> str:
        try:
            resp = self._get(f"/api/v1/repos/{owner}/{repo}")
            if resp.status_code == 200:
                return resp.json().get("updated_at", "")[:16]
        except Exception:
            pass
        return ""

    def fetch_issues(self, owner: str, repo: str) -> str:
        parts: list[str] = []
        for state in ["closed", "open"]:
            page = 1
            while True:
                resp = self._get(f"/api/v1/repos/{owner}/{repo}/issues",
                    {"type": "issues", "state": state, "limit": 50, "page": page})
                if resp.status_code != 200:
                    break
                issues = resp.json()
                if not issues:
                    break
                for issue in issues:
                    num = issue["number"]
                    title = issue["title"]
                    body = (issue.get("body") or "").strip()[:1500]
                    labels = ", ".join(lb["name"] for lb in issue.get("labels", []))
                    header = f"ISSUE #{num} [{state}]: {title}"
                    if labels:
                        header += f"  [labels: {labels}]"
                    parts.append(header)
                    if body:
                        parts.append(f"Body: {body}")
                    if issue.get("comments", 0) > 0:
                        try:
                            cr = self._get(f"/api/v1/repos/{owner}/{repo}/issues/{num}/comments")
                            if cr.status_code == 200:
                                for c in cr.json():
                                    cbody = (c.get("body") or "").strip()[:800]
                                    if cbody:
                                        parts.append(f"  COMMENT: {cbody}")
                        except Exception:
                            pass
                    parts.append("")
                if len(issues) < 50:
                    break
                page += 1
        return "\n".join(parts).strip()

    def fetch_commits(self, owner: str, repo: str) -> str:
        parts: list[str] = []
        page = 1
        fetched = 0
        while fetched < MAX_COMMITS:
            resp = self._get(f"/api/v1/repos/{owner}/{repo}/commits", {"limit": 50, "page": page})
            if resp.status_code != 200:
                break
            commits = resp.json()
            if not commits:
                break
            for c in commits:
                sha = c.get("sha", "")[:8]
                msg = (c.get("commit", {}).get("message") or "").strip()
                author = c.get("commit", {}).get("author", {}).get("name", "")
                date = (c.get("commit", {}).get("author", {}).get("date") or "")[:10]
                if not msg or len(msg) < 10:
                    continue
                if any(msg.split("\n")[0].lower().startswith(p) for p in GIT_SKIP_SUBJECTS):
                    continue
                parts.append(f"COMMIT {sha} by {author} on {date}:\n{msg[:600]}")
                parts.append("")
            fetched += len(commits)
            if len(commits) < 50:
                break
            page += 1
        return "\n".join(parts).strip()


def get_gitea_credentials(base_url: str) -> tuple[str, str]:
    """Read Gitea credentials from git credential store (fallback when GITEA_TOKEN unset)."""
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=http\nhost={base_url.split('://')[-1]}\n\n",
            capture_output=True, text=True, timeout=10,
        )
        user, pw = "", ""
        for line in result.stdout.splitlines():
            if line.startswith("username="):
                user = line[9:]
            elif line.startswith("password="):
                pw = line[9:]
        return user, pw
    except Exception:
        return "", ""
