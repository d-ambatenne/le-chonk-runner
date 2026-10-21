"""GitHub in memory for the demo's tests: the REST endpoints the demo calls, over httpx.MockTransport, and
real bare git repositories as the remotes (point LECHONK_GIT_BASE at file://<remotes>)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    def __init__(self, remotes: Path, user: str = "denis") -> None:
        self.remotes = remotes
        self.user = user  # the account the token or the connector signs in with
        self.repos: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.labels: dict[str, dict[str, dict[str, Any]]] = {}
        self.comments: dict[tuple[str, int], list[str]] = {}
        self.pulls: dict[str, list[dict[str, Any]]] = {}
        self.releases: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_next: dict[str, int] = {}  # e.g. {"release": 1}: the next release fails with a 502
        self.refuse_repos = False  # True: making a repository is refused, as for the Mistral GitHub App
        self._numbers: dict[str, int] = {}

    # --- Setup ----------------------------------------------------------------------------------------------

    def add_repo(self, repo: str, default_branch: str = "main") -> Path:
        bare = self.remotes / f"{repo}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "--quiet", "--bare", f"--initial-branch={default_branch}", str(bare))
        self.repos[repo] = {"full_name": repo, "name": repo.split("/", 1)[1], "default_branch": default_branch,
                            "html_url": f"https://github.com/{repo}", "private": True}
        for store in (self.issues, self.pulls, self.releases):
            store[repo] = []
        self.labels[repo] = {}
        return bare

    def add_file(self, repo: str, path: str, text: str, ref: str = "main") -> None:
        self.files[(repo, ref, path)] = text

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def branch_file(self, repo: str, branch: str, path: str) -> str:
        return _git("--git-dir", str(self.remotes / f"{repo}.git"), "show", f"{branch}:{path}")

    # --- The API --------------------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.requests.append((request.method, path))
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if match and request.method == method:
                repo = match.groupdict().get("repo")
                if repo is not None and repo not in self.repos:
                    return httpx.Response(404, json={"message": "Not Found"})
                return handler(request, body, **match.groupdict())
        return httpx.Response(404, json={"message": f"no route for {request.method} {path}"})

    def _routes(self) -> list[tuple[str, str, Any]]:
        repo = r"/repos/(?P<repo>[^/]+/[^/]+)"
        return [
            ("GET", repo, lambda r, b, repo: httpx.Response(200, json=self.repos[repo])),
            ("GET", repo + r"/issues", self._list_issues),
            ("POST", repo + r"/issues", self._create_issue),
            ("GET", repo + r"/issues/(?P<n>\d+)", lambda r, b, repo, n: self._ok(self._issue(repo, int(n)))),
            ("PATCH", repo + r"/issues/(?P<n>\d+)", self._update_issue),
            ("POST", repo + r"/issues/(?P<n>\d+)/labels", self._add_labels),
            ("DELETE", repo + r"/issues/(?P<n>\d+)/labels/(?P<name>.+)", self._remove_label),
            ("POST", repo + r"/issues/(?P<n>\d+)/comments", self._comment),
            ("GET", repo + r"/issues/(?P<n>\d+)/comments",
             lambda r, b, repo, n: httpx.Response(200, json=[{"body": c} for c in self.comments.get((repo, int(n)), [])])),
            ("GET", repo + r"/labels", lambda r, b, repo: httpx.Response(200, json=list(self.labels[repo].values()))),
            ("POST", repo + r"/labels", self._create_label),
            ("GET", repo + r"/contents/(?P<path>.+)", self._contents),
            ("POST", repo + r"/pulls", self._create_pull),
            ("GET", repo + r"/pulls", self._list_pulls),
            ("PUT", repo + r"/pulls/(?P<n>\d+)/merge", self._merge_pull),
            ("POST", repo + r"/releases", self._create_release),
            ("GET", repo + r"/releases/tags/(?P<tag>.+)", self._release_by_tag),
            ("POST", repo + r"/releases/(?P<rid>\d+)/assets", self._upload_asset),
            ("GET", r"/search/issues", self._search),
            ("POST", r"/user/repos", self._create_repo),
        ]

    @staticmethod
    def _ok(value: Any) -> httpx.Response:
        return httpx.Response(200, json=value) if value is not None else httpx.Response(404, json={"message": "Not Found"})

    def _number(self, repo: str) -> int:
        self._numbers[repo] = self._numbers.get(repo, 0) + 1
        return self._numbers[repo]

    def _issue(self, repo: str, number: int) -> dict[str, Any] | None:
        return next((i for i in self.issues[repo] if i["number"] == number), None)

    def _list_issues(self, request: httpx.Request, body: Any, repo: str) -> httpx.Response:
        state = request.url.params.get("state", "open")
        wanted = set(filter(None, request.url.params.get("labels", "").split(",")))
        items = [i for i in self.issues[repo] if state == "all" or i["state"] == state]
        items = [i for i in items if wanted <= {label["name"] for label in i["labels"]}]
        return httpx.Response(200, json=items)

    def _create_issue(self, request: httpx.Request, body: Any, repo: str) -> httpx.Response:
        number = self._number(repo)
        issue = {
            "number": number, "title": body["title"], "body": body.get("body", ""), "state": "open",
            "labels": [{"name": n} for n in body.get("labels", [])], "html_url": f"https://github.com/{repo}/issues/{number}",
        }
        self.issues[repo].append(issue)
        return httpx.Response(201, json=issue)

    def _update_issue(self, request: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        issue = self._issue(repo, int(n))
        if issue is None:
            return httpx.Response(404, json={"message": "Not Found"})
        for key in ("title", "body", "state"):
            if key in body:
                issue[key] = body[key]
        if "labels" in body:
            issue["labels"] = [{"name": n} for n in body["labels"]]
        return httpx.Response(200, json=issue)

    def _add_labels(self, request: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        issue = self._issue(repo, int(n))
        names = [label["name"] for label in issue["labels"]]
        issue["labels"] += [{"name": name} for name in body["labels"] if name not in names]
        return httpx.Response(200, json=issue["labels"])

    def _remove_label(self, request: httpx.Request, body: Any, repo: str, n: str, name: str) -> httpx.Response:
        issue = self._issue(repo, int(n))
        if not any(label["name"] == name for label in issue["labels"]):
            return httpx.Response(404, json={"message": "Label does not exist"})
        issue["labels"] = [label for label in issue["labels"] if label["name"] != name]
        return httpx.Response(200, json=issue["labels"])

    def _comment(self, request: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        self.comments.setdefault((repo, int(n)), []).append(body["body"])
        return httpx.Response(201, json={"id": len(self.comments), "body": body["body"]})

    def _create_label(self, request: httpx.Request, body: Any, repo: str) -> httpx.Response:
        self.labels[repo][body["name"]] = body
        return httpx.Response(201, json=body)

    def _contents(self, request: httpx.Request, body: Any, repo: str, path: str) -> httpx.Response:
        ref = request.url.params.get("ref", "main")
        text = self.files.get((repo, ref, path))
        if text is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"type": "file", "path": path, "content": base64.b64encode(text.encode()).decode()})

    def _create_pull(self, request: httpx.Request, body: Any, repo: str) -> httpx.Response:
        number = self._number(repo)
        pull = {"number": number, "title": body["title"], "head": body["head"], "base": body["base"], "body": body["body"],
                "html_url": f"https://github.com/{repo}/pull/{number}", "merged": False}
        self.pulls[repo].append(pull)
        return httpx.Response(201, json=pull)

    def _list_pulls(self, request: httpx.Request, body: Any, repo: str) -> httpx.Response:
        head = request.url.params.get("head", "").split(":", 1)[-1]
        return httpx.Response(200, json=[p for p in self.pulls[repo] if not head or p["head"] == head])

    def _release_by_tag(self, request: httpx.Request, body: Any, repo: str, tag: str) -> httpx.Response:
        return self._ok(next((r for r in self.releases[repo] if r["tag_name"] == tag), None))

    def _merge_pull(self, request: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        pull = next(p for p in self.pulls[repo] if p["number"] == int(n))
        bare = str(self.remotes / f"{repo}.git")
        sha = _git("--git-dir", bare, "rev-parse", f"refs/heads/{pull['head']}")
        _git("--git-dir", bare, "update-ref", f"refs/heads/{pull['base']}", sha)  # a fast-forward stands in for a squash
        pull.update(merged=True, merged_at="2026-10-20T12:00:00Z", merge_commit_sha=sha)
        return httpx.Response(200, json={"sha": sha, "merged": True})

    def _create_release(self, request: httpx.Request, body: Any, repo: str) -> httpx.Response:
        if self.fail_next.get("release"):
            self.fail_next["release"] -= 1
            return httpx.Response(502, json={"message": "Bad Gateway"})
        if any(r["tag_name"] == body["tag_name"] for r in self.releases[repo]):
            return httpx.Response(422, json={"message": "Validation Failed: tag_name already_exists"})
        release = {"id": len(self.releases[repo]) + 1, **body, "assets": [],
                   "html_url": f"https://github.com/{repo}/releases/tag/{body['tag_name']}"}
        self.releases[repo].append(release)
        return httpx.Response(201, json=release)

    def _upload_asset(self, request: httpx.Request, body: Any, repo: str, rid: str) -> httpx.Response:
        release = next(r for r in self.releases[repo] if r["id"] == int(rid))
        name = request.url.params["name"]
        asset = {"name": name, "size": len(request.content), "content_type": request.headers.get("content-type"),
                 "browser_download_url": f"https://github.com/{repo}/releases/download/{release['tag_name']}/{name}"}
        release["assets"].append(asset)
        return httpx.Response(201, json=asset)

    def _create_repo(self, request: httpx.Request, body: Any) -> httpx.Response:
        if self.refuse_repos:  # as GitHub answers an app that may not create repositories
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        repo = f"{self.user}/{body['name']}"
        if repo in self.repos:
            return httpx.Response(422, json={"message": "Repository creation failed: name already exists on this account"})
        self.add_repo(repo)
        self.repos[repo]["private"] = bool(body.get("private"))
        return httpx.Response(201, json=self.repos[repo])

    def _search(self, request: httpx.Request, body: Any) -> httpx.Response:
        query = request.url.params.get("q", "")
        repo = re.search(r"repo:(\S+)", query).group(1)
        words = [w.lower() for w in query.split() if ":" not in w]
        items = [i for i in self.issues.get(repo, []) if any(w in (i["title"] + " " + i["body"]).lower() for w in words)]
        return httpx.Response(200, json={"total_count": len(items), "items": items})
