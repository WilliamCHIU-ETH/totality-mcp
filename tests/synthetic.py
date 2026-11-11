"""Entirely invented input: two fake videos, four numbered test rows.
No sampled text, identifiers, source fingerprints, or model embeddings.
"""
import hashlib,json,pathlib,sqlite3
import numpy as np
from common import canonical,DIM,MODEL

def write(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True)+'\n')

class CharacterTokenizer:
 """Test double ONLY. One character per token; not a provider tokenizer."""
 def encode(self,text,**kwargs):return list(text)

def make_source(root):
 root=pathlib.Path(root);videos=[];pending=[]
 for v in range(1,3):
  vid=f'FAKEVID{v:04d}'
  videos.append(dict(video_id=vid,title=f'測試影片 {v:03d}',duration_s=30,url=f'https://example.invalid/video/{vid}'))
  rows=[];original=[];chunks=[]
  for row in range(2):
   number=(v-1)*2+row+1;cid=f'fixture-source-{number:03d}'
   text=f'測試句 {number:03d} synthetic{number:03d}'
   rawpath=f'raw/{vid}/row-{row}.json';write(root/rawpath,{'synthetic':True,'text':text})
   rawhash=hashlib.sha256((root/rawpath).read_bytes()).hexdigest()
   rows.append(dict(video_id=vid,chunk_id=cid,source_segment_index=row,reader_text=text,reader_original_text=text,start=row*10,end=row*10+5,timing_status='estimated',raw_path=rawpath,raw_sha256=rawhash))
   original.append(dict(chunk_id=cid,source_segment_index=row,text_raw=text))
   chunks.append(dict(chunk_id=cid,idx=row,start=row*10))
  write(root/f'context/videos/{vid}/segments.json',rows)
  write(root/f'raw/{vid}/chunks.json',{'chunks':chunks})
  path=root/f'transcripts/{vid}/openai__whisper-1/segments.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
  path.write_text(''.join(canonical(x)+'\n' for x in original))
  write(path.parent/'quality-flags.json',dict(video_id=vid,items=[dict(chunk_id=chunks[0]['chunk_id'],source_segment_index=0,flags=['synthetic_flag'])]))
  pending.append(dict(id=f'FAKE-P{v}',video_id=vid,chunk_id=chunks[0]['chunk_id'],source_segment_index=0,alternatives=['測試候選 A','測試候選 B']))
 write(root/'manifest/videos.frozen.json',dict(count=len(videos),videos=videos))
 write(root/'context/pending.json',pending)
 write(root/'context/corrections.json',[])
 write(root/'context/normalization.json',{'policy':'synthetic-only'})
 hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}
 write(root/'context/source-sha256.json',hashes)
 return root

def add_vectors(data):
 """Deterministic basis vectors. NOT model-generated embeddings."""
 data=pathlib.Path(data);chunks=[json.loads(x) for x in (data/'chunks.jsonl').read_text().splitlines()]
 vectors=np.eye(len(chunks),DIM,dtype='<f4')
 np.save(data/'vectors.npy',vectors);write(data/'vector-ids.json',[c['chunk_id'] for c in chunks])
 db=sqlite3.connect(data/'embedding-cache.sqlite')
 db.execute('CREATE TABLE cache(hash TEXT,model TEXT,dim INT,vec BLOB,PRIMARY KEY(hash,model,dim))')
 for c,v in zip(chunks,vectors):db.execute('INSERT INTO cache VALUES(?,?,?,?)',(c['content_sha256'],MODEL,DIM,v.tobytes()))
 db.commit();db.close()
 write(data/'embedding-manifest.json',dict(synthetic_fixture=True,conservative_reserved_usd=0,model=MODEL,dimensions=DIM))
 return chunks
