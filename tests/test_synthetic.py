"""CPU-light regression tests using only invented numbered test sentences."""
import json,pathlib,sqlite3,sys,threading
import numpy as np,pytest
import common,build,store,query_embed,server
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
 assert not re.fullmatch(patterns[0],'invalid')

class FakePG:
 """In-memory SQL boundary double; never connects or contains real sources."""
 def __init__(self,release,manifest,videos,chunks):
  self.closed=False;self.active=(release,manifest,videos);self.chunks=chunks;self.result=[];self.reads=0
 def execute(self,sql,args=()):
  if 'JOIN rag.releases' in sql:
   self.result=[self.active] if self.active else [];self.reads+=1
  elif 'SELECT payload' in sql:
   if 'chunk_id=' in sql:self.result=[(c,) for c in self.chunks if c['chunk_id']==args[0] and c['release_id']==args[1]]
   else:self.result=[(c,) for c in self.chunks if c['release_id']==args[0] and (args[1] is None or c['video_id']==args[1])]
  elif 'to_tsquery' in sql:self.result=[]
  else:raise AssertionError('unexpected synthetic SQL')
  return self
 def fetchone(self):return self.result[0] if self.result else None
 def fetchall(self):return list(self.result)
 def __iter__(self):return iter(self.result)

def test_pg_metadata_switch_rollback_and_filtered_search(sample,monkeypatch):
 _,_,chunks=sample
 old='r1-'+'1'*20;new='r1-'+'2'*20
 a=dict(chunks[0],release_id=old);b=dict(chunks[2],release_id=new)
 videos=[dict(video_id=a['video_id']),dict(video_id=b['video_id'])]
 s=object.__new__(store.Store);s.backend='postgres';s.videos={};s.release=None;s.index_vectors=None
 s._lock=threading.RLock();s.db=FakePG(old,dict(videos=1,chunks=1),videos[:1],[a,b]);s.refresh()
 assert list(s.videos)==[a['video_id']]
 assert s.search(b['text'],video_id=b['video_id'])['status']=='unknown_video'
 s.db.active=(new,dict(videos=2,chunks=1),videos)
 # Refresh must precede membership validation, not follow it.
 result=s.search(b['text'],video_id=b['video_id'])
 assert result['results'][0]['chunk_id']==b['chunk_id'] and result['release_id']==new
 assert s.get(b['chunk_id'])==b and len(s.videos)==2
 s.db.active=(old,dict(videos=1,chunks=1),videos[:1])
 assert s.get(b['chunk_id']) is None and len(s.videos)==1
 assert s.search(b['text'],video_id=b['video_id'])['status']=='unknown_video'
 s.db.active=(new,dict(videos=2,chunks=1),None)
 with pytest.raises(RuntimeError,match='metadata missing'):s.refresh()
 s.db.active=None
 with pytest.raises(RuntimeError,match='metadata missing'):s.refresh()

def test_search_pins_once_even_if_pointer_changes(sample):
 _,_,chunks=sample
 old='r1-'+'1'*20;new='r1-'+'2'*20;c=dict(chunks[0],release_id=old)
 s=object.__new__(store.Store);s.backend='postgres';s.videos={};s.release=None;s.index_vectors=None
 class SwitchingPG(FakePG):
  def execute(self,sql,args=()):
   result=super().execute(sql,args)
   if 'JOIN rag.releases' in sql:self.active=(new,dict(videos=0,chunks=0),[])
   return result
 s._lock=threading.RLock();s.db=SwitchingPG(old,dict(videos=1,chunks=1),[dict(video_id=c['video_id'])],[c])
 result=s.search(c['text'],video_id=c['video_id'])
 assert result['release_id']==old and result['results'][0]['chunk_id']==c['chunk_id']
 assert s.db.reads==1

def test_server_pg_list_health_follow_same_metadata(sample,monkeypatch):
 _,_,chunks=sample
 old='r1-'+'1'*20;new='r1-'+'2'*20
 videos=[dict(video_id=c['video_id']) for c in [chunks[0],chunks[2]]]
 s=object.__new__(store.Store);s.backend='postgres';s.release=None;s.videos={};s.manifest={}
 s._lock=threading.RLock();s.db=FakePG(old,dict(videos=1,chunks=1),videos[:1],[])
 monkeypatch.setattr(server,'_store',s)
 assert server.health()['videos']==1 and server.list_videos()['release_id']==old
 s.db.active=(new,dict(videos=2,chunks=2),videos)
 assert server.list_videos()['total']==2 and server.health()['release_id']==new
 s.db.active=(old,dict(videos=1,chunks=1),videos[:1])
 assert server.list_videos()['total']==1 and server.health()['chunks']==1

def test_activation_common_cas_parameters():
 from import_pg import activate_release
 class DB:
  def execute(self,sql,args):self.sql,self.args=sql,args
 db=DB();activate_release(db,'candidate','expected',False,True)
 assert db.args==('candidate','expected',False,True)
 assert 'rag.activate_release' in db.sql

def test_import_no_ddl_and_integrity(sample,monkeypatch):
 import import_pg
 _,data,_=sample;monkeypatch.setattr(import_pg,'DATA',data)
 class Result:
  def fetchone(self):return (False,)
 class DB:
  def __enter__(self):return self
  def __exit__(self,*a):return False
  def execute(self,sql,args=()):
   assert not any(x in sql.upper() for x in ['CREATE ','DELETE ','DROP ','ALTER '])
   calls.append((sql,args));return Result()
 calls=[];monkeypatch.setattr(import_pg.psycopg,'connect',lambda *a,**kw:DB())
 monkeypatch.setattr(import_pg,'register_vector',lambda *a:None)
 dsn=data/'fake-secret';dsn.write_text('synthetic-only')
 import_pg.run(dsn,True,None,True)
 assert sum('rag.put_chunk' in sql for sql,args in calls)==4
 assert calls[-1][1][-2:]==(True,False)
 (data/'chunks.jsonl').write_text('tampered')
 with pytest.raises((ValueError,json.JSONDecodeError)):import_pg.run(dsn)

def test_release_schema_and_deployment_contract():
 import yaml
 schema=(common.BASE/'schema.sql').read_text()
 migration=(common.BASE/'deploy/migrate-release-v2.sql').read_text()
 assert schema.endswith(migration)
 assert 'pg_advisory_xact_lock' in migration and 'CAS base mismatch' in migration
 assert 'candidate base mismatch' in migration and 'initial activation requires' in migration
 assert 'SECURITY DEFINER SET search_path=' in migration
 assert 'FROM PUBLIC' in migration and 'TO rag_loader' in migration
 assert 'OWNER TO rag_owner' in migration
 c=yaml.safe_load((common.BASE/'compose.yaml').read_text())
 assert c['services']['loader']['secrets']==['loader_dsn']
 assert '--activate' not in c['services']['loader']['command']
 # yaml 1.1 treats 'on' as boolean; account for both parsers.
 deploy=yaml.safe_load((common.BASE/'.github/workflows/deploy.yml').read_text())
 trigger=deploy.get('on',deploy.get(True))
 assert {'store.py','server.py','import_pg.py'}<=set(trigger['push']['paths'])
 docker=(common.BASE/'Dockerfile').read_text()
 assert all(name in docker for name in ['store.py','server.py','import_pg.py'])
 ci=(common.BASE/'.github/workflows/ci.yml').read_text()
 assert 'loader_password' in ci and 'loader_dsn' in ci

def test_server_list_and_health_refresh(sample,monkeypatch):
 import server
 _,_,chunks=sample;s=store.Store();monkeypatch.setattr(server,'_store',s)
 try:
  assert server.list_videos()['total']==2
  assert server.list_videos(offset=101)['videos']==[]
  assert server.health()['videos']==2
  assert server.get_passage(chunks[0]['chunk_id'])['status']=='ok'
  for offset in [-1,1000001]:
   with pytest.raises(ValueError):server.list_videos(offset=offset)
 finally:s.db.close()

def test_rich_citations_resources_and_prompts(sample,monkeypatch):
 import server
 _,_,chunks=sample;s=store.Store();monkeypatch.setattr(server,'_store',s)
 try:
  res=server.search_transcripts('synthetic001',2)
  first=res['results'][0]
  assert 'citation_markdown' in first and 'verification_badge' in first and 'time_range_display' in first
  assert first['listen_url'] in first['citation_markdown']
  p=server.get_passage(chunks[0]['chunk_id'])['passage']
  assert p['citation_markdown']==first['citation_markdown']
  summary=json.loads(server.catalog_summary())
  assert summary['total_videos']==2 and summary['total_chunks']==4
  videos=json.loads(server.catalog_videos())
  assert videos['total']==2 and len(videos['videos'])==2
  outline=json.loads(server.video_outline(chunks[0]['video_id']))
  assert outline['status']=='ok' and outline['passage_count']==2
  assert json.loads(server.video_outline('FAKEUNKNOWN'))['status']=='unknown_video'
  with pytest.raises(ValueError):server.video_outline('bad!')
  assert 'search_transcripts' in server.explore_concept('測試')
  assert 'get_passage' in server.verify_quote('測試',chunks[0]['video_id'])
  assert 'totality://videos/' in server.video_study_guide(chunks[0]['video_id'])
 finally:s.db.close()

def test_citation_badges_ranges_and_source_fields(sample):
 _,_,chunks=sample;s=store.Store()
 try:
  for flags,pending,expected in [
   (['outside_source_chunk'],[], '機器辨識（時間軸邊界回退）'),
   (['beyond_video_duration'],[], '機器辨識（時間軸邊界回退）'),
   (['not_audio_verified'],[{'alternatives':['synthetic']}], '機器辨識+上下文校訂（含待判候選詞）'),
   (['not_audio_verified'],[], '機器辨識+上下文校訂（未人工聽音）'),
   ([],[], '已校訂'),
  ]:
   c=dict(chunks[0],start=3661,end=3670,quality_flags=flags,pending=pending)
   full=s.public(c);short=s.public(c,full=False)
   assert full['verification_badge']==expected
   assert full['time_range_display']=='1:01:01–1:01:10'
   assert full['citation_markdown']==f"[{c['title']} (1:01:01–1:01:10)]({c['listen_url']}) · `{expected}`"
   assert full['quality_flags']==flags and full['pending']==pending
   assert full['source_refs']==c['source_refs'] and 'source_refs' not in short
   assert full['citation_markdown']==short['citation_markdown']
  assert store._fmt_mmss(-1)=='00:00'
 finally:s.db.close()

def test_sdk_registers_resources_and_prompts_without_network():
 import asyncio
 prompts=asyncio.run(server.mcp.list_prompts())
 resources=asyncio.run(server.mcp.list_resources())
 templates=asyncio.run(server.mcp.list_resource_templates())
 assert {p.name for p in prompts}=={'explore_concept','verify_quote','video_study_guide'}
 assert {str(r.uri) for r in resources}=={'totality://catalog/summary','totality://catalog/videos'}
 assert {t.uriTemplate for t in templates}=={'totality://videos/{video_id}'}

def test_probe_unconfigured_gateway_skips_http_but_checks_source(tmp_path,monkeypatch,capsys):
 from tools import probe
 monkeypatch.delenv('RAG_PUBLIC_GATEWAY',raising=False)
 monkeypatch.setenv('RAG_PROJECT_DOCS',str(tmp_path))
 source=tmp_path/'rag/cf-gateway/src/gateway.ts';source.parent.mkdir(parents=True)
 source.write_text("isConsentRemembered X-Auth-Diag style-src 'unsafe-inline' googleOrigin")
 def deny(*args,**kwargs):raise AssertionError('unset gateway must not make HTTP requests')
 monkeypatch.setattr(probe.urllib.request,'urlopen',deny)
 probe.probe_edge_gateway()
 output=capsys.readouterr().out
 assert 'skipped: RAG_PUBLIC_GATEWAY is not configured' in output
 assert 'remembered_consent_fastpath' in output and 'structured_redacted_diagnostics' in output

def test_probe_configured_gateway_is_used(monkeypatch,capsys):
 from tools import probe
 from io import BytesIO
 from urllib.error import HTTPError
 monkeypatch.setenv('RAG_PUBLIC_GATEWAY','https://gateway.example.invalid/')
 monkeypatch.delenv('RAG_PROJECT_DOCS',raising=False)
 seen=[]
 class Reply(BytesIO):
  status=200
  headers={'cache-control':'no-store'}
 def respond(request,**kwargs):
  seen.append(request.full_url)
  if request.full_url.endswith('/authorize'):
   raise HTTPError(request.full_url,400,'synthetic',{'x-auth-diag':'synthetic'},None)
  return Reply(json.dumps({'status':'ok','issuer':'https://gateway.example.invalid','code_challenge_methods_supported':['S256']}).encode())
 monkeypatch.setattr(probe.urllib.request,'urlopen',respond)
 probe.probe_edge_gateway()
 assert seen==['https://gateway.example.invalid/healthz','https://gateway.example.invalid/.well-known/oauth-authorization-server','https://gateway.example.invalid/authorize']
 assert 'x-auth-diag=synthetic' in capsys.readouterr().out

def test_probe_git_preserves_stderr_on_failure(monkeypatch):
 from tools import probe
 from types import SimpleNamespace
 monkeypatch.setattr(probe.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout='',stderr='synthetic failure'))
 assert probe.git('status')==(1,'synthetic failure')
