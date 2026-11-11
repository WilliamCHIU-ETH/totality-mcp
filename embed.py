#!/usr/bin/env python3
"""Resumable embeddings. Durable conservative reservations bound ALL attempts to $1.
No full responses, input text, or credential values are logged. Cache holds only
content hash, model/dimension and validated float32 vectors.
"""
import argparse,contextlib,fcntl,json,math,os,pathlib,sqlite3,time
import httpx,numpy as np
from common import *
PRICE=0.02/1_000_000;BUDGET=1.0
KEY_PATH=os.environ.get('RAG_OPENAI_KEY_FILE')

def run(dry=False,cache_only=False):
 DATA.mkdir(parents=True,exist_ok=True)
 lock=(DATA/'embedding.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 chunks=[json.loads(l) for l in (DATA/'chunks.jsonl').read_text().splitlines()]
 unique={c['content_sha256']:c for c in chunks}
 db=sqlite3.connect(DATA/'embedding-cache.sqlite');db.execute('PRAGMA journal_mode=WAL')
 db.executescript('CREATE TABLE IF NOT EXISTS cache(hash TEXT,model TEXT,dim INT,vec BLOB,PRIMARY KEY(hash,model,dim)); CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,reserved_tokens INT,usage_tokens INT,status TEXT,request_id TEXT);')
 have={r[0] for r in db.execute('SELECT hash FROM cache WHERE model=? AND dim=?',(MODEL,DIM))}
 todo=[c for h,c in unique.items() if h not in have]
 initial_attempt_id=db.execute('SELECT COALESCE(MAX(id),0) FROM attempts').fetchone()[0]
 reserved=db.execute('SELECT COALESCE(SUM(reserved_tokens),0) FROM attempts').fetchone()[0]
 info=dict(total_chunks=len(chunks),unique_texts=len(unique),cached=len(have),remaining=len(todo),remaining_tokens=sum(c['token_count'] for c in todo),already_reserved_usd=reserved*PRICE)
 info['estimated_new_usd']=info['remaining_tokens']*PRICE;info['with_three_attempts_usd']=(reserved+info['remaining_tokens']*3)*PRICE
 write_json(DATA/'embedding-dry-run.json',info);print(json.dumps(info),flush=True)
 if cache_only and todo:
  db.close()
  raise RuntimeError('cache_only_missing: no key read and no API request; stop and report missing cache')
 if dry:
  db.close();return
 if info['with_three_attempts_usd']>BUDGET:raise RuntimeError('cost hard stop before batch')
 # Read key only for actual requests; never print it or include exception strings.
 if todo and not KEY_PATH:raise ValueError('RAG_OPENAI_KEY_FILE must be explicitly configured')
 key=pathlib.Path(KEY_PATH).read_text().strip() if todo else None
 with (httpx.Client(timeout=90) if todo else contextlib.nullcontext(None)) as client:
  for start in range(0,len(todo),32):
   batch=todo[start:start+32];tokens=sum(c['token_count'] for c in batch)
   for retry in range(3):
    # Reserve before sending. Interrupted/failed attempts retain reservation.
    reserve=math.ceil(tokens*1.02)+len(batch)
    used=db.execute('SELECT COALESCE(SUM(reserved_tokens),0) FROM attempts').fetchone()[0]
    if (used+reserve)*PRICE>BUDGET:raise RuntimeError('cost hard stop: no request sent')
    cursor=db.execute('INSERT INTO attempts(reserved_tokens,status) VALUES(?,?)',(reserve,'reserved'));attempt=cursor.lastrowid;db.commit()
    try:
     response=client.post('https://api.openai.com/v1/embeddings',headers={'Authorization':'Bearer '+key},json={'model':MODEL,'dimensions':DIM,'encoding_format':'float','input':[c['text'] for c in batch]})
    except httpx.HTTPError:
     db.execute('UPDATE attempts SET status=? WHERE id=?',('network_unknown',attempt));db.commit()
     if retry==2:raise RuntimeError('API network error; details suppressed') from None
     time.sleep(2**retry+1);continue
    rid=response.headers.get('x-request-id','')[:200]
    if response.status_code!=200:
     db.execute('UPDATE attempts SET status=?,request_id=? WHERE id=?',('http_'+str(response.status_code),rid,attempt));db.commit()
     if response.status_code not in {429,500,502,503,504} or retry==2:raise RuntimeError('Embedding HTTP '+str(response.status_code)+'; body suppressed')
     time.sleep(min(60,max(2**retry+1,float(response.headers.get('retry-after','0')) if response.headers.get('retry-after','').isdigit() else 0)));continue
    payload=response.json();usage=int(payload['usage']['total_tokens'])
    if usage>reserve:raise RuntimeError('usage exceeds conservative reservation; stop')
    data=sorted(payload['data'],key=lambda x:x['index'])
    if len(data)!=len(batch) or [x['index'] for x in data]!=list(range(len(batch))):raise RuntimeError('invalid response count/index')
    if payload.get('model')!=MODEL:raise RuntimeError('unexpected embedding model')
    for c,item in zip(batch,data):
     vec=np.asarray(item['embedding'],dtype='<f4')
     if vec.shape!=(DIM,) or not np.isfinite(vec).all() or np.linalg.norm(vec)==0:raise RuntimeError('invalid embedding')
     db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',(c['content_sha256'],MODEL,DIM,vec.tobytes()))
    db.execute('UPDATE attempts SET usage_tokens=?,status=?,request_id=? WHERE id=?',(usage,'ok',rid,attempt));db.commit()
    del payload,data,response
    print(json.dumps(dict(completed=min(start+32,len(todo)),remaining_total=len(todo),usage_tokens=usage)),flush=True)
    time.sleep(0.25);break
 # Export deterministically, mapping evidence identities to text-shared cache.
 vectors=[];ids=[]
 for c in chunks:
  row=db.execute('SELECT vec FROM cache WHERE hash=? AND model=? AND dim=?',(c['content_sha256'],MODEL,DIM)).fetchone()
  if row is None:raise RuntimeError('incomplete cache')
  vectors.append(np.frombuffer(row[0],dtype='<f4'));ids.append(c['chunk_id'])
 np.save(DATA/'vectors.npy',np.stack(vectors));write_json(DATA/'vector-ids.json',ids)
 attempts=[dict(id=r[0],reserved_tokens=r[1],usage_tokens=r[2],status=r[3],request_id=r[4]) for r in db.execute('SELECT * FROM attempts')]
 usage=sum(x['usage_tokens'] or 0 for x in attempts);reserved=sum(x['reserved_tokens'] for x in attempts)
 report=dict(release_id=load(DATA/'build-manifest.json')['release_id'],export_mode='cache_only' if cache_only else 'resumable',new_api_requests=sum(1 for x in attempts if x['id']>initial_attempt_id),model=MODEL,dimensions=DIM,chunks=len(ids),unique_embeddings=len(unique),api_success_count=sum(x['status']=='ok' for x in attempts),usage_tokens=usage,estimated_cost_usd=usage*PRICE,conservative_reserved_usd=reserved*PRICE,budget_usd=BUDGET,not_supplier_bill=True,vectors_sha256=digest((DATA/'vectors.npy').read_bytes()),vector_ids_sha256=digest((DATA/'vector-ids.json').read_bytes()),cache_file='embedding-cache.sqlite',attempts=attempts)
 write_json(DATA/'embedding-manifest.json',report);db.close();print(json.dumps({k:v for k,v in report.items() if k!='attempts'}),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--dry-run',action='store_true');p.add_argument('--cache-only',action='store_true',help='fail before key/network access if any embedding is missing');a=p.parse_args();run(a.dry_run,a.cache_only)
