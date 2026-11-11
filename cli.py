#!/usr/bin/env python3
"""Local operator CLI. Never exposes arbitrary SQL or files over MCP."""
import argparse,json
from common import *
from store import Store
p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
s=sub.add_parser('search');s.add_argument('query');s.add_argument('--limit',type=int,default=5);s.add_argument('--video-id');s.add_argument('--query-vector-file',help='1536 values from compatible text-embedding-3-small model; no model API called here');s.add_argument('--exclude-flag',action='append',default=[])
g=sub.add_parser('get');g.add_argument('chunk_id')
sub.add_parser('health')
a=p.parse_args();store=Store()
if a.action=='search':out=store.search(a.query,a.limit,a.video_id,a.exclude_flag,load(a.query_vector_file) if a.query_vector_file else None)
elif a.action=='get':
 c=store.get(a.chunk_id);out={'status':'ok','passage':store.public(c)} if c else {'status':'not_found'}
else:out=dict(backend=store.backend,release_id=store.release,videos=len(store.videos),chunks=store.manifest['chunks'],audio_verified=False)
print(json.dumps(out,ensure_ascii=False,indent=2))
