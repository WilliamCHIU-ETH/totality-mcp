"""CPU-light regression tests using only invented numbered test sentences."""
import json,pathlib,sqlite3,sys
import numpy as np,pytest
import common,build,store,query_embed
from synthetic import make_source,add_vectors,CharacterTokenizer

@pytest.fixture
def sample(tmp_path,monkeypatch):
 source=make_source(tmp_path/'source');data=tmp_path/'data'
 monkeypatch.setattr(build,'DATA',data);monkeypatch.setattr(build,'CONTEXT','context')
 monkeypatch.setattr(build,'ENC',CharacterTokenizer())
 build.build(source)
 chunks=add_vectors(data)
 for module in [common,store,query_embed]:monkeypatch.setattr(module,'DATA',data)
 return source,data,chunks

def test_synthetic_build_and_source_integrity(sample):
 source,data,chunks=sample
 manifest=json.loads((data/'build-manifest.json').read_text())
 assert manifest['videos']==2 and manifest['chunks']==4
 assert manifest['source_counts']['rows']==4 and manifest['pending_cards_mapped']==2
 assert [c['text'] for c in chunks]==[f'測試句 {n:03d} synthetic{n:03d}' for n in range(1,5)]
 assert len({c['chunk_id'] for c in chunks})==4
 for c in chunks:
  assert c['content_sha256']==common.digest(c['text'].encode())
  assert c['untrusted_source'] and 'not_audio_verified' in c['quality_flags']
  for u in c['source_units']:
   row=next(r for r in c['source_refs'] if r['row_index']==u['row_index'])
   assert u['text']==row['reader_text'][u['char_start']:u['char_end']]
 assert all(common.digest((source/p).read_bytes())==h for p,h in json.loads((data/'sources.json').read_text())['sha256'].items())

def test_keyword_validation_and_read_only(sample):
 _,_,chunks=sample;s=store.Store()
 try:
  r=s.search('synthetic001',3)
  assert r['status']=='candidates_only' and r['results'][0]['chunk_id']==chunks[0]['chunk_id']
  assert r['answer_support']=='not_assessed' and not r['semantic_confidence_measured']
  assert s.search('missingterm9988')['status']=='no_evidence'
  assert s.search('test',video_id='FAKEUNKNOWN')['status']=='unknown_video'
  assert s.get("'; DROP TABLE chunks;--") is None
  for limit in [0,11,-1]:
   with pytest.raises(ValueError):s.search('test',limit)
  for q in ['', 'x'*1001]:
   with pytest.raises(ValueError):s.search(q)
  s.search("'; DROP TABLE chunks;--")
  with pytest.raises(sqlite3.OperationalError):s.db.execute('DELETE FROM chunks')
  assert len(s.rows())==4
 finally:s.db.close()

def test_partial_and_vector_signals(sample):
 _,data,chunks=sample;s=store.Store()
 try:
  r=s.search('synthetic001 unrelatedtoken4321')
  assert r['results'] and r['results'][0]['partial_keyword_support']
  assert 'unrelatedtoken4321' in r['results'][0]['unmatched_terms']
  vec=np.load(data/'vectors.npy')[0].tolist()
  r=s.search('absentlexeme',query_vector=vec)
  assert r['no_keyword_support'] and all(x['answer_support']=='not_assessed' for x in r['results'])
  assert r['results'][0]['chunk_id']==chunks[0]['chunk_id']
  for v in [[0.0]*common.DIM,[1.0]]:
   with pytest.raises(ValueError):s.search('x',query_vector=v)
 finally:s.db.close()

def test_pending_and_quality_evidence(sample):
 source,data,chunks=sample
 cards=json.loads((source/'context/pending.json').read_text())
 for i in [0,2]:
  c=chunks[i];assert c['pending'][0]['alternatives']==cards[i//2]['alternatives']
  assert c['pending_relations'][0]['relation']=='source_row'
  assert c['quality_flag_evidence'][0]['evidence']['flags']==['synthetic_flag']
  assert 'synthetic_flag' in c['quality_flags']
 s=store.Store()
 try:assert s.search('測試句',exclude_flags=['not_audio_verified'])['results']==[]
 finally:s.db.close()

def test_character_split_boundaries():
 build.ENC=CharacterTokenizer()
 text='測試句 001。'*100
 parts=list(build.split_text(text,max_tokens=32))
 assert ''.join(t for a,b,t in parts)==text
 assert all(text[a:b]==t and len(t)<=32 for a,b,t in parts)

def test_source_tamper_rejected(sample):
 source,data,_=sample
 (source/'context/videos/FAKEVID0001/segments.json').write_text('[]')
 with pytest.raises(ValueError,match='stage-one source mismatch'):build.build(source)

def test_cached_query_never_reads_key_or_uses_http(sample,monkeypatch):
 _,_,chunks=sample
 def deny(*a,**k):raise AssertionError('HTTP client must not be constructed')
 monkeypatch.setattr(query_embed.httpx,'Client',deny)
 v,kind=query_embed.query_embedding(chunks[0]['text'])
 assert kind=='corpus_cache' and v.shape==(common.DIM,)
 with pytest.raises(ValueError,match='disabled'):query_embed.query_embedding('uncachedsynthetic')

def test_cache_only_missing_stops_before_key_and_http(tmp_path,monkeypatch):
 import embed
 monkeypatch.setattr(embed,'DATA',tmp_path);monkeypatch.setattr(embed,'KEY_PATH',None)
 (tmp_path/'chunks.jsonl').write_text(common.canonical(dict(content_sha256='missing',token_count=1,text='測試句 999'))+'\n')
 def deny(*a,**k):raise AssertionError('HTTP client must not be constructed')
 monkeypatch.setattr(embed.httpx,'Client',deny)
 with pytest.raises(RuntimeError,match='cache_only_missing'):embed.run(cache_only=True)
 db=sqlite3.connect(tmp_path/'embedding-cache.sqlite')
 try:assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
 finally:db.close()

def test_explicit_key_required_before_http(tmp_path,monkeypatch):
 import embed
 monkeypatch.setattr(embed,'DATA',tmp_path);monkeypatch.setattr(embed,'KEY_PATH',None)
 (tmp_path/'chunks.jsonl').write_text(common.canonical(dict(content_sha256='missing',token_count=1,text='測試句 998'))+'\n')
 def deny(*a,**k):raise AssertionError('HTTP client must not be constructed')
 monkeypatch.setattr(embed.httpx,'Client',deny)
 with pytest.raises(ValueError,match='explicitly configured'):embed.run()

def test_compose_static():
 import yaml
 c=yaml.safe_load((common.BASE/'compose.yaml').read_text())
 assert 'ports' not in c['services']['rag-db']
 assert c['services']['rag-mcp']['ports']==['127.0.0.1:8765:8765']
 assert c['services']['rag-mcp']['read_only']
 assert c['networks']['db_internal']['internal']
 assert c['services']['loader']['profiles']==['import']

def test_offline_guard():
 import socket,subprocess
 with pytest.raises(AssertionError,match='prohibit'):socket.create_connection(('example.invalid',443))
 with pytest.raises(AssertionError,match='prohibit'):subprocess.Popen(['false'])


def test_release_activation_format(sample):
 import ast,re
 _,data,_=sample
 tree=ast.parse((common.BASE/'deploy/activate.py').read_text())
 patterns=[n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='fullmatch' and n.args and isinstance(n.args[0],ast.Constant)]
 assert len(patterns)==1
 assert re.fullmatch(patterns[0],json.loads((data/'build-manifest.json').read_text())['release_id'])
