"""Deterministic static boundary, de-identification, and hygiene checks."""
import ast
import hashlib
import pathlib
import re
import common

SKIP_DIR_NAMES = {'.git', '.venv', '.cache', '.ruff_cache', '.pytest_cache', '__pycache__', '.secrets'}
SKIP_ROOT_DIRS = {'source', 'backups'}
SKIP_FILES = {'.probe.env', '.host-sot.json', 'local.sqlite', 'chunks.jsonl', 'vectors.npy'}
BINARY_EXTS = {
    '.sqlite', '.db', '.npy', '.dump', '.log', '.pem', '.key',
    '.pyc', '.pyo', '.whl', '.tar', '.gz', '.zip', '.png', '.jpg', '.jpeg', '.webp', '.pdf', '.bin',
}
PROD_MODULES = ['common.py', 'store.py', 'server.py', 'query_embed.py', 'import_pg.py', 'build.py', 'embed.py', 'cli.py']
BLOCKED_3GRAM_HASHES = {
    '867e559a813f1ed8e0db39c8694f9f4a2e1527e402302bfa47f3922c3f47dbd9',
}
SECRET_PATTERNS = [
    re.compile(r'sk' + r'-proj-[A-Za-z0-9_-]{20,}'),
    re.compile(r'\bsk' + r'-[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgh' + r'p_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgithub_' + r'pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'BEGIN (?:RSA |OPENSSH |EC |)PRIVATE' + r' KEY'),
    re.compile(r'postgres(?:ql)?://[^\s\'"]+@(?!(?:localhost|127\.0\.0\.1|rag-db|example\.invalid)(?:[:/\s\'"]|$))[^\s\'"]+'),
    re.compile(r'/(?:home/ubuntu|data/OpenBot|Users)/'),
]
DEBUG_MARKERS = [
    'break' + 'point(',
    'pdb.' + 'set_trace',
    'TO' + 'DO',
    'FIX' + 'ME',
    'X' + 'XX',
    'HA' + 'CK',
]
YT_REAL_ID = re.compile(r'(?:youtube\.com/watch\?v=|youtu\.be/)(?!FAKEVID)[A-Za-z0-9_-]{11}')


def _repo_text_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for p in sorted(common.BASE.rglob('*')):
        if not p.is_file():
            continue
        rel = p.relative_to(common.BASE)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        if rel.parts[0] in SKIP_ROOT_DIRS:
            continue
        if rel.parts[0] == 'data' and rel.as_posix() != 'data/README.md':
            continue
        if p.name in SKIP_FILES or p.suffix.lower() in BINARY_EXTS:
            continue
        raw = p.read_bytes()
        if b'\x00' in raw:
            continue
        files.append(p)
    return files


def _salted_3gram(text: str) -> str:
    return hashlib.sha256(('totality-boundary-v1:' + text).encode()).hexdigest()


def _is_tool_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute) and target.attr == 'tool':
        return True
    if isinstance(target, ast.Name) and target.id == 'tool':
        return True
    return False


def _has_read_annotations(call: ast.Call) -> bool:
    kw = {k.arg: k.value for k in call.keywords}
    ann = kw.get('annotations')
    return isinstance(ann, ast.Name) and ann.id == 'READ'


def _verify_server_ast(src: str) -> None:
    tree = ast.parse(src)
    read_verified = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'READ' for t in node.targets):
            if isinstance(node.value, ast.Call):
                kw = {k.arg: k.value for k in node.value.keywords}
                ro = kw.get('readOnlyHint')
                dest = kw.get('destructiveHint')
                if isinstance(ro, ast.Constant) and ro.value is True and isinstance(dest, ast.Constant) and dest.value is False:
                    read_verified = True
    assert read_verified, 'READ must be assigned ToolAnnotations(readOnlyHint=True, destructiveHint=False, ...)'

    tool_count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if _is_tool_decorator(dec):
                    assert isinstance(dec, ast.Call) and _has_read_annotations(dec), (
                        f'tool {node.name} must use @mcp.tool(annotations=READ)'
                    )
                    tool_count += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_tool':
            assert _has_read_annotations(node), 'add_tool() must explicitly pass annotations=READ'
            tool_count += 1
    assert tool_count >= 4
    assert 'hmac.compare_digest' in src and 'allowed_origins' in src
    assert "RAG_ENABLE_QUERY_EMBEDDING')=='1'" in src


def test_gitignore_covers_sensitive_artifacts():
    lines = {line.strip() for line in (common.BASE / '.gitignore').read_text().splitlines() if line.strip() and not line.startswith('#')}
    for required in ['.secrets/', '*token*', '*.sqlite', '*.npy', '.env', '/data/*', '/source/']:
        assert required in lines, f'.gitignore missing {required}'


def test_no_leaked_identifiers_secrets_or_private_paths():
    for path in _repo_text_files():
        rel = path.relative_to(common.BASE).as_posix()
        text = path.read_text(encoding='utf-8', errors='ignore')
        for idx in range(max(0, len(text) - 2)):
            if _salted_3gram(text[idx : idx + 3]) in BLOCKED_3GRAM_HASHES:
                raise AssertionError(f'blocked private identifier hash matched in {rel}')
        for pat in SECRET_PATTERNS:
            assert not pat.search(text), f'secret or private path pattern matched in {rel}: {pat.pattern}'
        if path.suffix in {'.py', '.sql', '.yml', '.yaml', '.txt', '.cfg', '.ini', '.json5'} or path.name in {'Dockerfile', 'Makefile'}:
            assert not YT_REAL_ID.search(text), f'non-synthetic YouTube video ID in {rel}'


def test_production_modules_have_no_debug_or_placeholder_markers():
    for mod in PROD_MODULES:
        text = (common.BASE / mod).read_text(encoding='utf-8')
        for marker in DEBUG_MARKERS:
            assert marker not in text, f'disallowed marker {marker} found in {mod}'


def test_server_read_only_and_gate_invariants():
    src = (common.BASE / 'server.py').read_text(encoding='utf-8')
    _verify_server_ast(src)


def test_boundary_patterns_catch_bypass_variants():
    dsn_pat = SECRET_PATTERNS[5]
    assert dsn_pat.search('postgres' + 'ql://u:p@localhost.evil.example.net/db')
    assert dsn_pat.search('postgres' + 'ql://u:p@rag-db-prod.internal.corp/db')
    assert dsn_pat.search('postgres' + 'ql://u:p@127.0.0.1.nip.io/db')
    assert not dsn_pat.search('postgres' + 'ql://u:p@localhost:5432/db')
    assert not dsn_pat.search('postgres' + 'ql://u:p@127.0.0.1/db')

    base_ok = (
        'READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)\n'
        "hmac.compare_digest\nallowed_origins\nos.environ.get('RAG_ENABLE_QUERY_EMBEDDING')=='1'\n"
        + '\n'.join(f'@mcp.tool(annotations=READ)\ndef t{i}(): pass' for i in range(4))
    )
    _verify_server_ast(base_ok)
    for bad in [
        base_ok + '\n@mcp.tool\ndef sneak(): pass\n',
        base_ok + '\n@mcp.tool()\nasync def sneak(): pass\n',
        base_ok + '\nmcp.add_tool(sneak)\n',
    ]:
        try:
            _verify_server_ast(bad)
        except AssertionError:
            pass
        else:
            raise AssertionError(f'expected _verify_server_ast failure for:\n{bad}')
