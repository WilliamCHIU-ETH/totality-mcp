#!/usr/bin/env python3
"""Print the current state of this project from its sources of truth.

Run this before starting work instead of trusting notes or memory. It only
reads: git, the public GitHub API, the live MCP service, the Cloudflare Edge
OAuth gateway, and the sanitized Oracle host SOT snapshot (.host-sot.json).
It never prints secrets. Standard library only.

Optional settings come from the environment or an untracked `.probe.env`
file next to the repository root (KEY=VALUE lines):
  RAG_MCP_URL          default http://rag-mcp:8765/mcp
  RAG_MCP_TOKEN_FILE   default ~/.config/totality-rag/mcp_token
  RAG_PROJECT_DOCS     directory with decision/status notes (optional)
  RAG_PUBLIC_GATEWAY   optional public gateway URL; unset skips edge HTTP checks
"""
import datetime, hashlib, json, os, pathlib, re, subprocess, sys, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env():
    f = ROOT / ".probe.env"
    if f.is_file():
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def section(title):
    print(f"\n== {title}")


def git(*args):
    p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=15)
    return p.returncode, (p.stdout or p.stderr).strip()


def http_json(url, data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


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
    except Exception as e:
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
        prompts = [p["name"] for p in call("prompts/list").get("prompts", [])]
        resources = [str(r["uri"]) for r in call("resources/list").get("resources", [])]
        templates = [t["uriTemplate"] for t in call("resources/templates/list").get("resourceTemplates", [])]
        if prompts:
            print("prompts:", ", ".join(prompts))
        if resources or templates:
            print("resources:", ", ".join(resources + templates))
        sample_res = call("tools/call", {"name": "search_transcripts", "arguments": {"query": "修行", "limit": 1}})
        sample_data = sample_res.get("structuredContent") or json.loads(sample_res["content"][0]["text"])
        if sample_data.get("results"):
            r0 = sample_data["results"][0]
            rich = [k for k in ("time_range_display", "verification_badge", "citation_markdown") if k in r0]
            print("citation_fields:", ", ".join(rich) if rich else "legacy")
        print(f"endpoint: {url} (read-only; token in {token_file}, never print it)")
    except Exception as e:
        print(f"service unreachable at {url}: {type(e).__name__}: {str(e)[:80]}")


def _probe_edge_http(base):
    hdr = {"User-Agent": "totality-project-probe/1.0"}
    try:
        req = urllib.request.Request(f"{base}/healthz", headers=hdr)
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read().decode("utf-8", errors="replace"))
            print(f"healthz: HTTP {r.status} status={body.get('status')} cache-control={r.headers.get('cache-control')}")
        req_as = urllib.request.Request(f"{base}/.well-known/oauth-authorization-server", headers=hdr)
        with urllib.request.urlopen(req_as, timeout=8) as r:
            as_meta = json.loads(r.read().decode("utf-8", errors="replace"))
            print(f"oauth_metadata: issuer={as_meta.get('issuer')} pkce={as_meta.get('code_challenge_methods_supported')} cache-control={r.headers.get('cache-control')}")
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/authorize", headers=hdr), timeout=8)
        except urllib.error.HTTPError as err:
            diag = err.headers.get("x-auth-diag", "none")
            print(f"diagnostic_telemetry: active (GET /authorize without params -> HTTP {err.code}, x-auth-diag={diag})")
    except Exception as e:
        print(f"edge gateway check failed at {base}: {type(e).__name__}: {str(e)[:80]}")


def probe_edge_gateway():
    section("edge gateway (Cloudflare OAuth 2.1 gateway is the source of truth)")
    base = os.environ.get("RAG_PUBLIC_GATEWAY", "").strip().rstrip("/")
    if base:
        _probe_edge_http(base)
    else:
        print("edge HTTP checks skipped: RAG_PUBLIC_GATEWAY is not configured")

    docs = os.environ.get("RAG_PROJECT_DOCS")
    if docs:
        gw_src = pathlib.Path(docs) / "rag/cf-gateway/src/gateway.ts"
        if gw_src.is_file():
            code = gw_src.read_text(errors="replace")
            features = []
            if "isConsentRemembered" in code:
                features.append("remembered_consent_fastpath")
            if "X-Auth-Diag" in code:
                features.append("structured_redacted_diagnostics")
            if "style-src 'unsafe-inline'" in code and "googleOrigin" in code:
                features.append("modern_ui_and_csp_redirect_fix")
            print("gateway_source_features:", ", ".join(features))


def probe_host_sot():
    docs = os.environ.get("RAG_PROJECT_DOCS")
    if not docs:
        return
    sot_file = pathlib.Path(docs) / ".host-sot.json"
    if not sot_file.is_file():
        return
    section("oracle host snapshot (.host-sot.json exported by host cron)")
    try:
        data = json.loads(sot_file.read_text(errors="replace"))
        updated = datetime.datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
        age_min = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds() / 60
        stale = age_min > data.get("stale_after_minutes", 15)
        print(f"updated_at: {data['updated_at']} (age={age_min:.1f}m -> {'STALE' if stale else 'FRESH'})")
        for cname, cinfo in sorted(data.get("containers", {}).items()):
            print(f"container {cname}: {cinfo.get('status')} ({cinfo.get('image')})")
        host_shas = data.get("files_sha256", {})
        mismatches = []
        for fname, h_sha in sorted(host_shas.items()):
            local_p = ROOT / fname
            if local_p.is_file():
                l_sha = hashlib.sha256(local_p.read_bytes()).hexdigest()
                if l_sha != h_sha:
                    mismatches.append(fname)
        print("host_vs_repo_files:", "IN_SYNC (all tracked files match)" if not mismatches else f"DIFF in {', '.join(mismatches)}")
        if data.get("latest_backup"):
            print(f"latest_db_backup: {data['latest_backup']} (total={data.get('backup_count', 0)})")
    except Exception as e:
        print(f"host snapshot unreadable: {type(e).__name__}: {str(e)[:80]}")


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
    for step in (probe_repo, probe_ci, probe_service, probe_edge_gateway, probe_host_sot, probe_docs):
        try:
            step()
        except Exception as e:
            print(f"{step.__name__} failed: {type(e).__name__}: {str(e)[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
