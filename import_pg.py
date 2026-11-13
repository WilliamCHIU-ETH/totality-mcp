#!/usr/bin/env python3
"""Transactional candidate import. DDL is a separate owner-only step."""
import argparse,json,pathlib
import numpy as np,psycopg
from psycopg.types.json import Jsonb
from pgvector.psycopg import register_vector
from common import *

def activate_release(db,release,expected_base_release=None,allow_initial=False,rollback=False):
 db.execute('SELECT rag.activate_release(%s,%s,%s,%s)',(release,expected_base_release,allow_initial,rollback))

def run(dsnfile,activate=False,expected_base_release=None,allow_initial=False):
 chunks=[json.loads(l) for l in (DATA/'chunks.jsonl').read_text().splitlines()]
 manifest=load(DATA/'build-manifest.json');videos=load(DATA/'videos.json')
 ids=load(DATA/'vector-ids.json');vectors=np.load(DATA/'vectors.npy');em=load(DATA/'embedding-manifest.json')
 if (digest((DATA/'vectors.npy').read_bytes())!=em['vectors_sha256'] or
     digest((DATA/'chunks.jsonl').read_bytes())!=manifest['chunks_sha256'] or
     vectors.shape!=(len(chunks),DIM) or len(ids)!=len(set(ids)) or
     ids!=[c['chunk_id'] for c in chunks] or not np.isfinite(vectors).all() or
     np.any(np.linalg.norm(vectors,axis=1)==0) or
     digest((DATA/'vector-ids.json').read_bytes())!=em['vector_ids_sha256'] or
     em.get('model')!=MODEL or em.get('dimensions')!=DIM or
     len(videos)!=manifest['videos'] or len(chunks)!=manifest['chunks'] or
     len({v['video_id'] for v in videos})!=len(videos) or
     {c['video_id'] for c in chunks}!={v['video_id'] for v in videos} or
     any(c['release_id']!=manifest['release_id'] or digest(c['text'].encode())!=c['content_sha256'] for c in chunks)):
  raise ValueError('artifact integrity mismatch')
 if manifest.get('videos_sha256') and digest((DATA/'videos.json').read_bytes())!=manifest['videos_sha256']:
  raise ValueError('video metadata hash mismatch')
 with psycopg.connect(pathlib.Path(dsnfile).read_text().strip()) as db:
  register_vector(db)
  sealed=db.execute('SELECT rag.stage_release(%s,%s)',(Jsonb(manifest),Jsonb(videos))).fetchone()[0]
  if not sealed:
   for c,vec in zip(chunks,vectors):
    db.execute('SELECT rag.put_chunk(%s,%s,%s)',(Jsonb(c),' '.join(terms(index_text(c))),vec))
   db.execute('SELECT rag.seal_release(%s)',(manifest['release_id'],))
  if activate:activate_release(db,manifest['release_id'],expected_base_release,allow_initial)
 print(json.dumps(dict(imported=len(chunks),release_id=manifest['release_id'],activated=activate)))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--dsn-file',required=True);p.add_argument('--activate',action='store_true')
 p.add_argument('--expected-base-release');p.add_argument('--allow-initial',action='store_true')
 a=p.parse_args();run(a.dsn_file,a.activate,a.expected_base_release,a.allow_initial)
