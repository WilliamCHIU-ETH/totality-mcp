#!/usr/bin/env python3
"""Deterministic rebuild from read-only stage-one inputs; no API calls."""
import collections,json,os,pathlib,re,sqlite3
from common import *
os.environ.setdefault('TIKTOKEN_CACHE_DIR',str(BASE/'.cache/tiktoken'))
ENC=None
def nt(s):
 global ENC
 if ENC is None:
  import tiktoken
  ENC=tiktoken.get_encoding('cl100k_base')
 return len(ENC.encode(s,disallowed_special=()))
def split_text(text,max_tokens=340):
 # Unicode-safe offsets, preferring sentence/clause boundaries. No model rewriting.
 start=0
 for m in re.finditer(r'.+?(?:[。！？!?；;\n]|$)',text,re.S):
  a,b=m.span()
  while a<b:
   end=b
   if nt(text[a:end])>max_tokens:
    lo,hi=a+1,b
    while lo<hi:
     mid=(lo+hi+1)//2
     if nt(text[a:mid])<=max_tokens:lo=mid
     else:hi=mid-1
    end=lo
   yield a,end,text[a:end]
   a=end

def build(source=SOURCE):
 DATA.mkdir(parents=True,exist_ok=True)
 frozen={}
 def read(rel):
  p=(source/rel).resolve();p.relative_to(source.resolve())
  b=p.read_bytes();frozen[str(rel)]=digest(b);return json.loads(b)
 manifest=read(pathlib.Path('manifest/videos.frozen.json'))
 videos=manifest['videos'];assert manifest['count']==len(videos)>0 and len({v['video_id'] for v in videos})==len(videos)
 pending=read(pathlib.Path(CONTEXT)/'pending.json')
 corrections=read(pathlib.Path(CONTEXT)/'corrections.json')
 normalization=read(pathlib.Path(CONTEXT)/'normalization.json')
 # Verify the stage-one audit list rather than trusting recorded source hashes.
 previous=read(pathlib.Path(CONTEXT)/'source-sha256.json')
 verified=0
 for rel,expected in previous.items():
  p=(source/rel).resolve();p.relative_to(source.resolve())
  actual=digest(p.read_bytes())
  if actual!=expected:raise ValueError('stage-one source mismatch: '+rel)
  verified+=1
 allrows={};chunkspec={};transcripts={};rowcounts=collections.Counter();pending_seen=set()
 quality={};quality_counts=collections.Counter();quality_files=0
 for v in videos:
  vid=v['video_id'];rel=pathlib.Path(CONTEXT)/'videos'/vid/'segments.json'
  rows=read(rel);assert all(r['video_id']==vid for r in rows);allrows[vid]=(rel,rows)
  chunkspec[vid]={x['chunk_id']:x for x in read(pathlib.Path('raw')/vid/'chunks.json')['chunks']}
  tp=pathlib.Path('transcripts')/vid/'openai__whisper-1/segments.jsonl';blob=(source/tp).read_bytes();frozen[str(tp)]=digest(blob)
  transcripts[vid]={}
  for line in blob.splitlines():
   x=json.loads(line);transcripts[vid][(x['chunk_id'],x['source_segment_index'])]=x
  qpath=pathlib.Path('transcripts')/vid/'openai__whisper-1/quality-flags.json'
  quality[vid]=[]
  if (source/qpath).is_file():
   qdoc=read(qpath);assert qdoc['video_id']==vid;quality_files+=1
   for qi,item in enumerate(qdoc['items']):
    scope='source_row' if 'source_segment_index' in item else 'source_chunk'
    quality_counts.update((scope+':'+flag) for flag in item['flags'])
    quality[vid].append(dict(source_path=str(qpath),source_sha256=frozen[str(qpath)],item_index=qi,scope=scope,source_chunk_id=item['chunk_id'],source_segment_index=item.get('source_segment_index'),flags=item['flags'],evidence=item))
  for r in rows:
   if r['timing_status']=='outside_source_chunk':rowcounts['outside_source_chunk']+=1
   if r['start']<0 or r['end']>v['duration_s']:rowcounts['beyond_video_duration']+=1
   rowcounts['rows']+=1
   if r.get('correction_id'):rowcounts['context_corrected']+=1
 # Freeze all actual raw JSON referenced by reader rows.
 for _,rows in allrows.values():
  for r in rows:
   rel=r['raw_path']
   if rel not in frozen:
    actual=digest((source/rel).read_bytes())
    if actual!=r['raw_sha256']:raise ValueError('raw hash mismatch: '+rel)
    frozen[rel]=actual
 release='r1-'+digest(canonical(frozen).encode())[:20]
 output=[];coverage=collections.Counter()
 for v in videos:
  vid=v['video_id'];rel,rows=allrows[vid];units=[]
  for index,row in enumerate(rows):
   text=row['reader_text']
   for a,b,textpart in split_text(text):units.append(dict(row_index=index,char_start=a,char_end=b,text=textpart,source_chunk=row['chunk_id']))
  pos=0
  while pos<len(units):
   end=pos;parts=[]
   while end<len(units) and units[end]['source_chunk']==units[pos]['source_chunk']:
    candidate='\n'.join(parts+[units[end]['text']])
    if parts and nt(candidate)>350:break
    parts.append(units[end]['text']);end+=1
   if end==pos:raise ValueError('chunker made no progress')
   chosen=units[pos:end];idxs=sorted(set(u['row_index'] for u in chosen)); selected=[rows[i] for i in idxs]
   flags={'not_audio_verified'}
   if any(r['timing_status']=='outside_source_chunk' for r in selected):flags.add('outside_source_chunk')
   if any(r['start']<0 or r['end']>v['duration_s'] for r in selected):flags.add('beyond_video_duration')
   if any(r.get('correction_id') for r in selected):flags.add('context_corrected')
   quality_evidence=[q for q in quality[vid] if any(q['source_chunk_id']==r['chunk_id'] and (q['scope']=='source_chunk' or q['source_segment_index']==r['source_segment_index']) for r in selected)]
   for q in quality_evidence:flags.update(q['flags'])
   cards=[];pending_relations=[]
   for pi,card in enumerate(pending):
    if card['video_id']!=vid:continue
    match=any((card.get('chunk_id')==r['chunk_id'] and card.get('source_segment_index')==r['source_segment_index']) or r['chunk_id'] in card.get('source_chunks',[]) for r in selected)
    if match:
     cards.append(dict(pending_index=pi,**card));pending_seen.add(pi)
     direct=any(card.get('chunk_id')==r['chunk_id'] and card.get('source_segment_index')==r['source_segment_index'] for r in selected)
     alt_match=any(a in r['reader_text'] or a in r['reader_original_text'] for a in card.get('alternatives',[]) for r in selected)
     pending_relations.append(dict(pending_index=pi,relation='source_row' if direct else ('alternative_text_in_source_row' if alt_match else 'context_only'),notice='Context-only means the source chunk is associated; the disputed phrase is not located in this passage.' if not direct and not alt_match else 'Referenced source row contains the pending item or an alternative; no alternative was selected.'))
   if cards:flags.add('pending_review')
   source_chunk=chunkspec[vid][units[pos]['source_chunk']]
   if source_chunk['idx']>0:flags.add('source_chunk_overlap_possible')
   uncertain=bool(flags & {'outside_source_chunk','beyond_video_duration'})
   safe_start=source_chunk['start'] if uncertain else min(r['start'] for r in selected)
   if not 0<=safe_start<=v['duration_s']:safe_start=None
   text='\n'.join(parts)
   identity=[vid,release,VERSION,[(u['row_index'],u['char_start'],u['char_end']) for u in chosen]]
   cid='c_'+digest(canonical(identity).encode())
   refs=[]
   for i in idxs:
    r=rows[i];tr=transcripts[vid].get((r['chunk_id'],r['source_segment_index']))
    if tr is None:raise ValueError('missing transcript row')
    refs.append(dict(row_index=i,source_path=str(rel),source_sha256=frozen[str(rel)],source_chunk_id=r['chunk_id'],source_segment_index=r['source_segment_index'],start=r['start'],end=r['end'],timing_status=r['timing_status'],raw_path=r['raw_path'],raw_sha256=r['raw_sha256'],asr_text=tr['text_raw'],reader_original_text=r['reader_original_text'],reader_text=r['reader_text'],correction_id=r.get('correction_id')))
    coverage[(vid,i)]+=1
   output.append(dict(chunk_id=cid,video_id=vid,release_id=release,title=v['title'],duration_s=v['duration_s'],text=text,content_sha256=digest(text.encode()),embedding_model=MODEL,embedding_dimensions=DIM,normalization_version=normalization['policy'],chunker_version=VERSION,token_count=nt(text),source_units=chosen,source_refs=refs,start=min(r['start'] for r in selected),end=max(r['end'] for r in selected),time_status=sorted(set(r['timing_status'] for r in selected)),citation_start=safe_start,citation_precision='source_chunk_start_only' if uncertain and safe_start is not None else ('source_segment_estimate' if safe_start is not None else 'unavailable'),source_url=v['url'],listen_url=v['url']+(f'&t={int(safe_start)}s' if safe_start is not None else ''),quality_flags=sorted(flags),quality_flag_evidence=quality_evidence,pending=cards,pending_relations=pending_relations,untrusted_source=True))
   if end==len(units) or units[end]['source_chunk']!=units[pos]['source_chunk']:pos=end
   else:
    overlap=end;tail=[]
    while overlap>pos+1 and nt('\n'.join([units[overlap-1]['text']]+tail))<=50:
     overlap-=1;tail.insert(0,units[overlap]['text'])
    pos=overlap
 assert len(coverage)==rowcounts['rows'],('coverage',len(coverage),rowcounts)
 assert len(pending_seen)==len(pending),('pending not mapped',set(range(len(pending)))-pending_seen)
 assert len({x['chunk_id'] for x in output})==len(output)
 with (DATA/'chunks.jsonl').open('w') as f:
  for c in output:f.write(canonical(c)+'\n')
 write_json(DATA/'videos.json',videos);write_json(DATA/'sources.json',dict(release_id=release,sha256=frozen,stage_one_hashes_verified=verified))
 unique={c['content_sha256']:c for c in output};tokens=sum(c['token_count'] for c in unique.values())
 report=dict(release_id=release,videos=len(videos),chunks=len(output),unique_embedding_texts=len(unique),source_counts=dict(rowcounts),pending_cards=len(pending),pending_cards_mapped=len(pending_seen),corrected_source_records=len(corrections),source_files=len(frozen),quality_flag_files=quality_files,source_quality_item_flag_counts=dict(quality_counts),stage_one_hashes_verified=verified,chunk_tokens=sum(c['token_count'] for c in output),uncached_tokens=tokens,estimated_usd=tokens*0.02/1e6,retry_reserved_usd=tokens*0.02/1e6*3,budget_usd=1,model=MODEL,dimensions=DIM,chunks_sha256=digest((DATA/'chunks.jsonl').read_bytes()),flag_chunk_counts=dict(collections.Counter(f for c in output for f in c['quality_flags'])),max_chunk_tokens=max(c['token_count'] for c in output),notes=['350 token ceiling; sentence/row-boundary overlap up to 50 tokens, may be zero for long units','Source text is not audio verified; estimated time is not precise alignment'])
 if report['retry_reserved_usd']>1:raise ValueError('dry-run exceeds budget')
 write_json(DATA/'build-manifest.json',report)
 # SQLite is an explicitly local test backend, not the production DB.
 path=DATA/'local.sqlite'
 if path.exists():path.unlink()
 db=sqlite3.connect(path);db.executescript('CREATE TABLE chunks(id TEXT PRIMARY KEY,video_id TEXT,body TEXT,flags TEXT,payload TEXT); CREATE VIRTUAL TABLE keywords USING fts5(id UNINDEXED, tokens);')
 for c in output:
  db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)',(c['chunk_id'],c['video_id'],c['text'],canonical(c['quality_flags']),canonical(c)))
  db.execute('INSERT INTO keywords VALUES(?,?)',(c['chunk_id'],' '.join(terms(index_text(c)))))
 db.commit();db.close();print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':build()
