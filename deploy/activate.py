#!/usr/bin/env python3
"""Owner-only atomic release switch/rollback; preserves both releases."""
import argparse,pathlib,re,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent.parent))
import psycopg
p=argparse.ArgumentParser();p.add_argument('--dsn-file',required=True);p.add_argument('--release',required=True);a=p.parse_args()
if not re.fullmatch(r'r1-[a-f0-9]{20}',a.release):raise SystemExit('invalid release')
with psycopg.connect(pathlib.Path(a.dsn_file).read_text().strip()) as db:
 row=db.execute('SELECT manifest FROM rag.releases WHERE release_id=%s',(a.release,)).fetchone()
 if not row:raise SystemExit('unknown release')
 count=db.execute('SELECT count(*) FROM rag.chunks WHERE release_id=%s',(a.release,)).fetchone()[0]
 if count!=row[0]['chunks']:raise SystemExit('incomplete release')
 db.execute('INSERT INTO rag.active_release VALUES(true,%s) ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id',(a.release,))
print('Active release switched atomically; old release retained. Restart only MCP to refresh all cached metadata.')
