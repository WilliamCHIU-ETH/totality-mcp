"""Optional query adapter, OFF by default. Corpus cache lookup is free.
Enable only when query-text external processing is explicitly acceptable.
A distinct writable query ledger plus immutable corpus reservations enforces a
combined $1 local-program ceiling; never logs query text or full responses.
"""
import fcntl,math,os,pathlib,sqlite3,time
import httpx,numpy as np
from common import *
PRICE=0.02/1_000_000

def query_embedding(text):
 h=digest(text.encode())
 # Existing corpus vectors may be reused, with no API/key access.
 source=DATA/'embedding-cache.sqlite'
 if source.exists():
  db=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
  row=db.execute('SELECT vec FROM cache WHERE hash=? AND model=? AND dim=?',(h,MODEL,DIM)).fetchone();db.close()
  if row:return np.frombuffer(row[0],dtype='<f4').copy(),'corpus_cache'
 if os.environ.get('RAG_ENABLE_QUERY_EMBEDDING')!='1':raise ValueError('query embedding API disabled')
 ledger=pathlib.Path(os.environ.get('RAG_QUERY_LEDGER_DIR',str(BASE/'.cache/queries')))
 ledger.mkdir(parents=True,exist_ok=True)
 with (ledger/'lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  db=sqlite3.connect(ledger/'query-cache.sqlite')
  try:
   db.executescript('CREATE TABLE IF NOT EXISTS cache(hash TEXT PRIMARY KEY,model TEXT,dim INT,vec BLOB); CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,reserved_tokens INT,usage_tokens INT,status TEXT,request_id TEXT);')
   row=db.execute('SELECT vec FROM cache WHERE hash=? AND model=? AND dim=?',(h,MODEL,DIM)).fetchone()
   if row:return np.frombuffer(row[0],dtype='<f4').copy(),'query_cache'
   # UTF-8 byte count safely bounds cl100k tokens without downloading tokenizer
   # data into the server container. Reserved on every attempt, not only success.
   reserve=len(text.encode('utf-8'))+8
   corpus_reserved=load(DATA/'embedding-manifest.json')['conservative_reserved_usd']
   key=None
   with httpx.Client(timeout=30) as client:
    for retry in range(3):
     used=db.execute('SELECT COALESCE(SUM(reserved_tokens),0) FROM attempts').fetchone()[0]
     if corpus_reserved+(used+reserve)*PRICE>1:raise ValueError('combined embedding cost hard stop: request not sent')
     attempt=db.execute('INSERT INTO attempts(reserved_tokens,status) VALUES(?,?)',(reserve,'reserved')).lastrowid;db.commit()
     if key is None:
      keypath=os.environ.get('RAG_OPENAI_KEY_FILE')
      if not keypath:raise ValueError('RAG_OPENAI_KEY_FILE must be explicitly configured')
      key=pathlib.Path(keypath).read_text().strip()
     try:r=client.post('https://api.openai.com/v1/embeddings',headers={'Authorization':'Bearer '+key},json={'model':MODEL,'dimensions':DIM,'encoding_format':'float','input':text})
     except httpx.HTTPError:
      db.execute('UPDATE attempts SET status=? WHERE id=?',('network_unknown',attempt));db.commit()
      if retry==2:raise ValueError('query embedding network error') from None
      time.sleep(2**retry);continue
     rid=r.headers.get('x-request-id','')[:200]
     if r.status_code!=200:
      db.execute('UPDATE attempts SET status=?,request_id=? WHERE id=?',('http_'+str(r.status_code),rid,attempt));db.commit()
      if r.status_code not in {429,500,502,503,504} or retry==2:raise ValueError('query embedding HTTP '+str(r.status_code))
      time.sleep(2**retry);continue
     p=r.json();usage=int(p['usage']['total_tokens']);v=np.asarray(p['data'][0]['embedding'],dtype='<f4')
     if p.get('model')!=MODEL or v.shape!=(DIM,) or not np.isfinite(v).all() or np.linalg.norm(v)==0 or usage>reserve:raise ValueError('invalid query embedding response')
     db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',(h,MODEL,DIM,v.tobytes()));db.execute('UPDATE attempts SET usage_tokens=?,status=?,request_id=? WHERE id=?',(usage,'ok',rid,attempt));db.commit()
     return v,'query_api'
  finally:db.close()
 raise ValueError('query embedding retries exhausted')
