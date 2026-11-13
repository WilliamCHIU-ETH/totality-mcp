#!/usr/bin/env python3
"""Owner-only one-time metadata backfill for an existing v1 release.
Run migration first. Refuses mismatched or changed metadata; never switches data.
"""
import argparse,json,pathlib
import psycopg
from psycopg.types.json import Jsonb

def main():
 p=argparse.ArgumentParser();p.add_argument('--dsn-file',required=True);p.add_argument('--release',required=True);p.add_argument('--videos-file',required=True);a=p.parse_args()
 videos=json.loads(pathlib.Path(a.videos_file).read_text());ids=[v['video_id'] for v in videos]
 if len(ids)!=len(set(ids)):raise SystemExit('duplicate video IDs')
 with psycopg.connect(pathlib.Path(a.dsn_file).read_text().strip()) as db:
  row=db.execute('SELECT manifest,videos FROM rag.releases WHERE release_id=%s FOR UPDATE',(a.release,)).fetchone()
  if not row:raise SystemExit('release missing')
  actual={r[0] for r in db.execute('SELECT DISTINCT video_id FROM rag.chunks WHERE release_id=%s',(a.release,))}
  if actual!=set(ids) or len(ids)!=row[0]['videos']:raise SystemExit('metadata/chunks mismatch')
  if row[1] is not None and row[1]!=videos:raise SystemExit('immutable metadata conflict')
  db.execute('UPDATE rag.releases SET videos=%s WHERE release_id=%s',(Jsonb(videos),a.release))
 print('Release metadata verified and backfilled; no pointer change.')
if __name__=='__main__':main()
