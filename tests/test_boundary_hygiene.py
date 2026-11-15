"""Deterministic static boundary, de-identification, and hygiene checks."""
import ast
import hashlib
import pathlib
import re
import common

SKIP_DIRS = {'.git', '.venv', '.cache', '.ruff_cache', '.pytest_cache', '__pycache__', '.secrets', 'source', 'backups'}
SKIP_FILES = {'.probe.env', '.host-sot.json', 'local.sqlite', 'chunks.jsonl', 'vectors.npy'}
TEXT_EXTS = {'.py', '.yml', '.yaml', '.sql', '.md', '.json', '.toml', '.sh', '.example'}
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
    re.compile(r'postgres(?:ql)?://[^\s\'"]+@(?!(?:localhost|127\.0\.0\.1|rag-db|example\.invalid)\b)[^\s\'"]+'),
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
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.parts[0] == 'data' and rel.as_posix() != 'data/README.md':
            continue
        if p.name in SKIP_FILES or p.suffix in {'.sqlite', '.db', '.npy', '.dump', '.log', '.pem', '.key'}:
            continue
        if p.suffix in TEXT_EXTS or p.name in {'Dockerfile', '.dockerignore', '.gitignore', 'requirements.lock'}:
            files.append(p)
    return files


def _salted_3gram(text: str) -> str:
    return hashlib.sha256(('totality-boundary-v1:' + text).encode()).hexdigest()


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
        if path.suffix in {'.py', '.sql', '.yml', '.yaml'} or path.name == 'Dockerfile':
            assert not YT_REAL_ID.search(text), f'non-synthetic YouTube video ID in {rel}'


def test_production_modules_have_no_debug_or_placeholder_markers():
    for mod in PROD_MODULES:
        text = (common.BASE / mod).read_text(encoding='utf-8')
        for marker in DEBUG_MARKERS:
            assert marker not in text, f'disallowed marker {marker} found in {mod}'


def test_server_read_only_and_gate_invariants():
    src = (common.BASE / 'server.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    tool_count = 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'tool':
                    tool_count += 1
                    kw = {k.arg: k.value for k in dec.keywords}
                    assert 'annotations' in kw and isinstance(kw['annotations'], ast.Name) and kw['annotations'].id == 'READ', (
                        f'tool {node.name} must use annotations=READ'
                    )
    assert tool_count >= 4
    assert 'readOnlyHint=True' in src and 'destructiveHint=False' in src
    assert 'hmac.compare_digest' in src and 'allowed_origins' in src
    assert "RAG_ENABLE_QUERY_EMBEDDING')=='1'" in src
