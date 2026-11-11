#!/usr/bin/env python3
"""Transactional, idempotent production import. Owner DSN only from a secret file."""
import argparse,json,pathlib
import numpy as np,psycopg
from psycopg.types.json import Jsonb
from pgvector.psycopg import register_vector
from common import *
def run(dsnfile,activate=False):
 dsn=pathlib.Path(dsnfile).read_text().strip()
 chunks=[json.loads(l) for l in (DATA/'chunks.jsonl').read_text().splitlines()];manifest=load(DATA/'build-manifest.json');ids=load(DATA/'vector-ids.json');vectors=np.load(DATA/'vectors.npy')
 em=load(DATA/'embedding-manifest.json')
 assert digest((DATA/'vectors.npy').read_bytes())==em['vectors_sha256']
 assert digest((DATA/'chunks.jsonl').read_bytes())==manifest['chunks_sha256']
 assert vectors.shape==(len(chunks),DIM) and len(ids)==len(set(ids))
 assert ids==[c['chunk_id'] for c in chunks] and np.isfinite(vectors).all()
 with psycopg.connect(dsn) as db:
  db.execute((BASE/'schema.sql').read_text());register_vector(db)
  db.execute('INSERT INTO rag.releases(release_id,manifest) VALUES(%s,%s) ON CONFLICT(release_id) DO NOTHING',(manifest['release_id'],Jsonb(manifest)))
  for c,vec in zip(chunks,vectors):
   db.execute("INSERT INTO rag.chunks(chunk_id,release_id,video_id,content_sha256,model,dimensions,payload,keywords,embedding) VALUES(%s,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s) ON CONFLICT(chunk_id) DO NOTHING",(c['chunk_id'],c['release_id'],c['video_id'],c['content_sha256'],MODEL,DIM,Jsonb(c),' '.join(terms(index_text(c))),vec))
  count=db.execute('SELECT count(*) FROM rag.chunks WHERE release_id=%s',(manifest['release_id'],)).fetchone()[0]
  assert count==len(chunks)
  if activate:db.execute('INSERT INTO rag.active_release VALUES(true,%s) ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id',(manifest['release_id'],))
 print(json.dumps(dict(imported=count,release_id=manifest['release_id'],activated=activate)))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--dsn-file',required=True);p.add_argument('--activate',action='store_true');args=p.parse_args();run(args.dsn_file,args.activate)
