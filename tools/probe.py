#!/usr/bin/env python3
"""Print the current state of this project from its sources of truth.

Run this before starting work instead of trusting notes or memory. It only
reads: git, the public GitHub API, and the MCP health tool. It never prints
secrets. Standard library only.

Optional settings come from the environment or an untracked `.probe.env`
file next to the repository root (KEY=VALUE lines):
  RAG_MCP_URL          default http://rag-mcp:8765/mcp
  RAG_MCP_TOKEN_FILE   default ~/.config/totality-rag/mcp_token
  RAG_PROJECT_DOCS     directory with decision/status notes (optional)
"""
import json, os, pathlib, re, subprocess, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env():
    f = ROOT / ".probe.env"
    if f.is_file():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def git(*args):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or r.stderr).strip()


def http_json(url, data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def section(title):
    print(f"\n== {title}")


def probe_repo():
    section("repo (source of truth for code)")
    _, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    _, head = git("log", "-1", "--format=%h %cs %s")
    print(f"branch: {branch} | head: {head}")
    code, _ = git("fetch", "--quiet", "origin")
    if code != 0:
        print("fetch: FAILED (remote state below may be stale)")
    for ref in ("origin/dev", "origin/main"):
        code, out = git("rev-list", "--left-right", "--count", f"HEAD...{ref}")
        if code == 0:
            ahead, behind = out.split()
            print(f"vs {ref}: ahead {ahead}, behind {behind}")
    _, dirty = git("status", "--porcelain")
    n = len([l for l in dirty.splitlines() if l.strip()])
    print(f"uncommitted files: {n}")
    code, out = git("rev-list", "--count", "origin/main..origin/dev")
    if code == 0:
        print(f"commits on dev not yet on main (not in production): {out}")
    code, out = git("push", "--dry-run", "origin", "HEAD:refs/heads/dev")
    print("push to dev:", "allowed" if code == 0 else "NOT allowed (" + out.splitlines()[-1][:80] + ")")
    print("push to main: always rejected by branch protection; production changes go through a pull request")


def repo_slug():
    _, url = git("remote", "get-url", "origin")
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def probe_ci():
    section("CI and deployment (GitHub is the source of truth)")
    slug = repo_slug()
    if not slug:
        print("cannot read repository name from the remote"); return
    api = f"https://api.github.com/repos/{slug}/actions"
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": "project-probe"}
    try:
        runs = http_json(f"{api}/runs?branch=dev&per_page=1", headers=hdr)["workflow_runs"]
        if runs:
            r = runs[0]
            print(f"dev CI: {r['conclusion'] or r['status']} at {r['head_sha'][:7]} ({r['created_at']})")
        runs = http_json(f"{api}/workflows/deploy.yml/runs?branch=main&per_page=10", headers=hdr)["workflow_runs"]
        if runs:
            r = runs[0]
            state = r["conclusion"] or r["status"]
            note = " <- waiting for the owner's approval" if r["status"] == "waiting" else ""
            print(f"latest deploy run: {state} at {r['head_sha'][:7]} ({r['created_at']}){note}")
        ok = next((r for r in runs if r["conclusion"] == "success"), None)
        print("in production:", f"{ok['head_sha'][:7]} (deployed {ok['updated_at']})" if ok else "no successful deploy found in the last 10 runs")
    except Exception as e:  # network or rate limit
        print(f"GitHub API unavailable: {type(e).__name__}: {str(e)[:80]}")


def probe_service():
    section("live service (the MCP itself is the source of truth)")
    url = os.environ.get("RAG_MCP_URL", "http://rag-mcp:8765/mcp")
    token_file = pathlib.Path(os.environ.get("RAG_MCP_TOKEN_FILE", "~/.config/totality-rag/mcp_token")).expanduser()
    if not token_file.is_file():
        print(f"token file missing: {token_file}"); return
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "Authorization": "Bearer " + token_file.read_text().strip()}

    def call(method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return http_json(url, data=json.dumps(body).encode(), headers=hdr)["result"]

    try:
        h = json.loads(call("tools/call", {"name": "health", "arguments": {}})["content"][0]["text"])
        keys = ("status", "release_id", "videos", "chunks", "backend", "read_only", "automatic_query_api")
        print("health:", ", ".join(f"{k}={h[k]}" for k in keys if k in h))
        print("tools:", ", ".join(t["name"] for t in call("tools/list")["tools"]))
        print(f"endpoint: {url} (read-only; token in {token_file}, never print it)")
    except Exception as e:
        print(f"service unreachable at {url}: {type(e).__name__}: {str(e)[:80]}")


def probe_docs():
    docs = os.environ.get("RAG_PROJECT_DOCS")
    if not docs:
        return
    section("decision notes (owner decisions; code cannot tell you these)")
    d = pathlib.Path(docs)
    now = time.time()
    for f in sorted(d.glob("RAG-*.md")):
        if ".before-" in f.name:
            continue
        text = f.read_text(errors="replace")[:600]
        m = re.search(r'last_verified:\s*"?([0-9-]+)', text)
        age = int((now - f.stat().st_mtime) // 86400)
        print(f"{f}: last_verified={m.group(1) if m else 'none'}, modified {age}d ago")
    print("if a note disagrees with the sections above, the sections above win; fix the note")


def main():
    load_env()
    print("project probe:", time.strftime("%Y-%m-%d %H:%M %Z"))
    for step in (probe_repo, probe_ci, probe_service, probe_docs):
        try:
            step()
        except Exception as e:
            print(f"{step.__name__} failed: {type(e).__name__}: {str(e)[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
