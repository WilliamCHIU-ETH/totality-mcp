"""Shared immutable IDs, validation and JSON helpers; no secrets in outputs."""
import hashlib,json,os,pathlib,re,unicodedata
BASE=pathlib.Path(__file__).resolve().parent
DATA=pathlib.Path(os.environ.get('RAG_DATA',str(BASE/'data'))).resolve()
SOURCE=pathlib.Path(os.environ.get('RAG_SOURCE',str(BASE/'source'))).resolve()
CONTEXT=os.environ.get('RAG_CONTEXT_DIR','context')
MODEL='text-embedding-3-small'
DIM=1536
VERSION='sentences-350-o50-v1'
def digest(b):return hashlib.sha256(b).hexdigest()
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def write_json(path,x):
 path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
def load(path):return json.loads(pathlib.Path(path).read_text())
def norm(s):return unicodedata.normalize('NFKC',s).casefold()
def terms(s):
 s=norm(s);out=re.findall(r'[a-z0-9āīūṛṃḥśṣñṭḍṇ]+|[\u3400-\u9fff]+',s)
 result=[]
 for t in out:
  if re.fullmatch(r'[\u3400-\u9fff]+',t):result.extend([t] if len(t)==1 else [t[i:i+2] for i in range(len(t)-1)])
  else:result.append(t)
 return list(dict.fromkeys(result))
def index_fields(chunk):
 # Field-specific support must refer to exactly the strings indexed by retrieval.
 return dict(corrected_text=chunk['text'],title=chunk['title'],pending_alternatives='\n'.join(a for p in chunk.get('pending',[]) for a in p.get('alternatives',[])),pending_candidates='\n'.join(a for p in chunk.get('pending',[]) for a in p.get('candidates',[])),asr_source_rows='\n'.join(r.get('asr_text','') for r in chunk.get('source_refs',[])))
def index_text(chunk):
 # Search alternatives as alternatives, never overwrite corrected evidence.
 return '\n'.join(index_fields(chunk).values())
def keyword_support(chunk,query,query_terms):
 fields=index_fields(chunk)
 field_terms={name:set(terms(text)) for name,text in fields.items()}
 matched_fields={name:[t for t in query_terms if t in tokens] for name,tokens in field_terms.items()}
 matched_fields={k:v for k,v in matched_fields.items() if v}
 matched=[t for t in query_terms if any(t in hit for hit in matched_fields.values())]
 phrase=norm(query.strip());exact=[name for name,text in fields.items() if phrase and phrase in norm(text)]
 kind='exact_corrected_phrase' if 'corrected_text' in exact else ('exact_indexed_phrase' if exact else ('all_searched_terms_present' if query_terms and len(matched)==len(query_terms) else ('partial_keyword_overlap' if matched else 'no_keyword_support')))
 return dict(matched_terms=matched,unmatched_terms=[t for t in query_terms if t not in matched],matched_fields=matched_fields,exact_phrase_fields=exact,match_type=kind,no_keyword_support=not bool(matched),partial_keyword_support=bool(matched) and len(matched)<len(query_terms),candidate_only=True,answer_support='not_assessed',rrf_score_kind='rank_fusion_not_confidence')
def checked_query(query,limit,video_id,exclude_flags):
 if not isinstance(query,str) or not 1<=len(query.strip())<=1000:raise ValueError('query length must be 1..1000')
 if type(limit)!=int or not 1<=limit<=10:raise ValueError('limit must be integer 1..10')
 if video_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):raise ValueError('invalid video_id')
 if len(exclude_flags)>20 or any(not re.fullmatch(r'[a-z_]{1,64}',x) for x in exclude_flags):raise ValueError('invalid exclude_flags')
