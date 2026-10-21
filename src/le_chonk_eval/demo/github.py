"""GitHub for the demo: the REST calls it needs, and git clone and push on this Mac.

The token (GITHUB_TOKEN in .env, a fine-grained token for the demo repo) stays on this machine: git gets it
as a one-off header, so it's never written to .git/config, and the containers where Vibe works never see it.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .issues import LABELS

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Never synced from Vibe's project into the repo, nor deleted from it: build output and local state.
EXCLUDED = ("build", ".kotlin", ".gradle", ".git", ".idea", "DerivedData", "node_modules", ".amper")
EXCLUDED_SUFFIXES = (".apk",)
GITIGNORE = "build/\n.kotlin/\n.gradle/\n.idea/\n.amper/\nDerivedData/\n*.apk\n.DS_Store\n"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def parse_repo(value: str) -> str:
    """owner/name from a GitHub URL or a slug."""
    value = value.strip().strip("<>").removesuffix(".git").rstrip("/")
    if _SLUG.match(value):
        return value
    parsed = urlparse(value if "://" in value else f"https://{value}")
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc.lower() not in ("github.com", "www.github.com") or len(parts) < 2:
        raise ValueError(f"Not a GitHub repository: {value!r}. Use https://github.com/<owner>/<repo>.")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def parse_skill_url(value: str) -> dict[str, str]:
    """repo, ref and folder of a skill folder's GitHub URL: https://github.com/<owner>/<repo>/tree/<ref>/<folder>."""
    parsed = urlparse(value.strip().strip("<>"))
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc.lower() not in ("github.com", "www.github.com") or len(parts) < 5 or parts[2] != "tree":
        raise ValueError("A skill is a link to its folder on GitHub: https://github.com/<owner>/<repo>/tree/<branch>/<folder>")
    return {"repo": f"{parts[0]}/{parts[1]}", "ref": parts[3], "path": "/".join(parts[4:]), "url": value.strip().strip("<>")}


class GitHub:
    def __init__(self, token: str | None = None, *, api: str | None = None, uploads: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.api = (api or os.environ.get("LECHONK_GITHUB_API") or API).rstrip("/")
        self.uploads = (uploads or os.environ.get("LECHONK_GITHUB_UPLOADS") or UPLOADS).rstrip("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "le-chonk-demo"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.http = httpx.AsyncClient(headers=headers, timeout=60, transport=transport)

    async def close(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> GitHub:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def request(self, method: str, path: str, *, base: str | None = None, **kwargs: Any) -> Any:
        response = await self.http.request(method, f"{base or self.api}{path}", **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise GitHubError(response.status_code, str(message)[:500])
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- Repository and issues ------------------------------------------------------------------------------

    async def repo(self, repo: str) -> dict[str, Any]:
        return await self.request("GET", f"/repos/{repo}")

    async def create_repo(self, name: str, description: str = "") -> dict[str, Any]:
        """An empty private repository in the token's account. A fine-grained token for one repo can't do this."""
        return await self.request("POST", "/user/repos", json={"name": name, "description": description, "private": True, "auto_init": False})

    async def issues(self, repo: str, state: str = "open", labels: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        """Issues only (GitHub lists pull requests among them), oldest first."""
        found: list[dict[str, Any]] = []
        page = 1
        while len(found) < limit:
            params: dict[str, Any] = {"state": state, "per_page": 100, "page": page, "sort": "created", "direction": "asc"}
            if labels:
                params["labels"] = labels
            batch = await self.request("GET", f"/repos/{repo}/issues", params=params)
            found += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                break
            page += 1
        return found[:limit]

    async def issue(self, repo: str, number: int) -> dict[str, Any]:
        return await self.request("GET", f"/repos/{repo}/issues/{number}")

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return await self.request("POST", f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": labels})

    async def update_issue(self, repo: str, number: int, **fields: Any) -> dict[str, Any]:
        return await self.request("PATCH", f"/repos/{repo}/issues/{number}", json=fields)

    async def relabel(self, repo: str, number: int, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        for label in remove or []:
            try:
                await self.request("DELETE", f"/repos/{repo}/issues/{number}/labels/{quote(label, safe='')}")
            except GitHubError as exc:
                if exc.status != 404:
                    raise
        if add:
            await self.request("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": list(add)})

    async def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return await self.request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    async def search_issues(self, repo: str, words: str, limit: int = 20) -> list[dict[str, Any]]:
        query = f"repo:{repo} is:issue {words}"
        found = await self.request("GET", "/search/issues", params={"q": query, "per_page": limit})
        return list((found or {}).get("items") or [])

    async def ensure_labels(self, repo: str) -> None:
        existing = {label["name"] for label in await self.request("GET", f"/repos/{repo}/labels", params={"per_page": 100})}
        for name, (color, description) in LABELS.items():
            if name not in existing:
                await self.request("POST", f"/repos/{repo}/labels", json={"name": name, "color": color, "description": description})

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str | None:
        try:
            found = await self.request("GET", f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref} if ref else None)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(found, dict) or found.get("type") != "file":
            return None
        return base64.b64decode(found.get("content") or "").decode("utf-8", errors="replace")

    # --- Pull requests and releases -------------------------------------------------------------------------

    async def create_pull(self, repo: str, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return await self.request("POST", f"/repos/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})

    async def find_pull(self, repo: str, head: str) -> dict[str, Any] | None:
        """The pull request from branch `head` of the repo, open or closed: a retried step reuses it."""
        owner = repo.split("/", 1)[0]
        found = await self.request("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{head}", "state": "all", "per_page": 1})
        return found[0] if found else None

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        return await self.request("GET", f"/repos/{repo}/pulls/{number}")

    async def merge_pull(self, repo: str, number: int, title: str) -> dict[str, Any]:
        return await self.request("PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": "squash", "commit_title": title})

    async def create_release(self, repo: str, tag: str, target: str, name: str, body: str, prerelease: bool = False) -> dict[str, Any]:
        return await self.request("POST", f"/repos/{repo}/releases", json={
            "tag_name": tag, "target_commitish": target, "name": name, "body": body, "prerelease": prerelease,
        })

    async def release_by_tag(self, repo: str, tag: str) -> dict[str, Any] | None:
        try:
            return await self.request("GET", f"/repos/{repo}/releases/tags/{quote(tag, safe='')}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    async def ensure_release(self, repo: str, tag: str, target: str, name: str, body: str, prerelease: bool = False) -> dict[str, Any]:
        """The release of `tag`, made if it isn't there: a retried step reuses it."""
        return await self.release_by_tag(repo, tag) or await self.create_release(repo, tag, target, name, body, prerelease)

    async def upload_asset(self, repo: str, release: dict[str, Any], path: Path, name: str, content_type: str) -> dict[str, Any]:
        existing = next((a for a in release.get("assets") or [] if a.get("name") == name), None)
        if existing:  # uploaded by an earlier try of the same step
            return existing
        return await self.request(
            "POST", f"/repos/{repo}/releases/{release['id']}/assets", base=self.uploads, params={"name": name},
            content=path.read_bytes(), headers={"Content-Type": content_type},
        )


async def check_skill(github: GitHub, url: str) -> dict[str, str]:
    """A skill link's name and description, read from its SKILL.md: also checks that the folder is a skill."""
    source = parse_skill_url(url)
    text = await github.file_text(source["repo"], f"{source['path']}/SKILL.md", ref=source["ref"])
    if text is None:
        raise ValueError(f"No SKILL.md in {url}")
    meta = _frontmatter(text)
    return {"url": source["url"], "name": meta.get("name") or source["path"].rsplit("/", 1)[-1], "description": meta.get("description", "")}


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        import yaml

        data = yaml.safe_load(text[3:end]) or {}
    except Exception:
        return {}
    return {k: " ".join(str(v).split()) for k, v in data.items() if k in ("name", "description")} if isinstance(data, dict) else {}


# --- git on this Mac ------------------------------------------------------------------------------------------

def clone_url(repo: str) -> str:
    return f"{os.environ.get('LECHONK_GIT_BASE', 'https://github.com').rstrip('/')}/{repo}.git"


def _auth(token: str | None) -> list[str]:
    if not token or not clone_url("x/y").startswith("https://"):
        return []
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {header}"]


def git(args: list[str], cwd: Path | None = None, token: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    # Not your git config: its hooks, signing or credential helpers would get in the bot's way.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Vibe (le-chonk)", "GIT_AUTHOR_EMAIL": "vibe@le-chonk.invalid",
           "GIT_COMMITTER_NAME": "le-chonk", "GIT_COMMITTER_EMAIL": "le-chonk@le-chonk.invalid"}
    result = subprocess.run(["git", *_auth(token), *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if token:
            detail = detail.replace(token, "***")
        raise RuntimeError(f"git {args[0]} failed: {detail[-1500:]}")
    return result


def clone(repo: str, dest: Path, token: str | None, branch: str) -> str | None:
    """Clone the repo's branch; returns its head commit, or None for an empty repository."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--quiet", clone_url(repo), str(dest)], token=token)
    if git(["rev-parse", "--verify", "--quiet", f"origin/{branch}"], cwd=dest, check=False).returncode != 0:
        git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=dest)  # empty: the branch is born with the first commit
        return None
    git(["checkout", "--quiet", "-B", branch, f"origin/{branch}"], cwd=dest)
    return head(dest)


def head(repo_dir: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()


FIRST_COMMIT = "Start the repository"


def is_fresh(repo_dir: Path) -> bool:
    """Only the first commit the demo made: as good as empty for Vibe's instruction."""
    log = git(["log", "--format=%s", "-n", "2"], cwd=repo_dir, check=False).stdout.splitlines()
    return log == [FIRST_COMMIT]


def reset_to(repo_dir: Path, commit: str) -> None:
    """Back to the base commit, without leftovers of an earlier try."""
    git(["reset", "--quiet", "--hard", commit], cwd=repo_dir)
    git(["clean", "--quiet", "-fdx"], cwd=repo_dir)


def initial_commit(repo_dir: Path, token: str | None, branch: str, title: str) -> str:
    """Give an empty repository a first commit, so tasks have a base branch to merge into."""
    (repo_dir / ".gitignore").write_text(GITIGNORE)
    (repo_dir / "README.md").write_text(f"# {title}\n\nBuilt by Vibe from the issues of this repository (le-chonk demo).\n")
    git(["add", "-A"], cwd=repo_dir)
    git(["commit", "--quiet", "-m", FIRST_COMMIT], cwd=repo_dir)
    git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo_dir, token=token)
    return head(repo_dir)


def _excluded(relative: Path) -> bool:
    return any(part in EXCLUDED for part in relative.parts) or relative.name.endswith(EXCLUDED_SUFFIXES)


def sync_tree(source: Path, dest: Path) -> None:
    """Make dest's files those of source, except .git and build output, which stay as they are."""
    for path in sorted(dest.rglob("*"), reverse=True):
        relative = path.relative_to(dest)
        if _excluded(relative) or path.is_dir():
            continue
        if not (source / relative).exists():
            path.unlink()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if _excluded(relative):
            continue
        target = dest / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for path in sorted(dest.rglob("*"), reverse=True):  # folders left empty
        if path.is_dir() and not _excluded(path.relative_to(dest)) and not any(path.iterdir()):
            path.rmdir()


def commit_all(repo_dir: Path, message: str) -> str | None:
    """Commit every change but build output; returns the commit, or None when there was nothing to commit."""
    excludes = [f":(exclude,glob)**/{name}/**" for name in EXCLUDED if name != ".git"]
    excludes += [f":(exclude,glob)**/*{suffix}" for suffix in EXCLUDED_SUFFIXES]
    git(["add", "-A", "--", ".", *excludes], cwd=repo_dir)
    if git(["diff", "--cached", "--quiet"], cwd=repo_dir, check=False).returncode == 0:
        return None
    git(["commit", "--quiet", "-m", message], cwd=repo_dir)
    return head(repo_dir)


def push(repo_dir: Path, branch: str, token: str | None) -> None:
    git(["push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo_dir, token=token)
