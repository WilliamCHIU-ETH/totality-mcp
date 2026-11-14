"""Offline connection lifecycle tests; no real DSNs, sockets or corpus."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import threading
import psycopg
import pytest
import store

ACTIVE=('synthetic-release',{'videos':1,'chunks':0},[{'video_id':'synthetic-video'}])

class Connection:
 def __init__(self,privileged=False,writable=False):
  self.closed=False;self.privileged=privileged;self.writable=writable
  self.failure=None;self.calls=[]
 def close(self):self.closed=True
 def execute(self,sql,params=None):
  self.calls.append(sql)
  if self.closed:raise psycopg.InterfaceError('synthetic closed connection')
  if self.failure:
   error,self.failure=self.failure,None
   if isinstance(error,psycopg.OperationalError) and not error.sqlstate:self.close()
   raise error
  if 'rolsuper' in sql:return Mock(fetchone=lambda:(self.privileged,False,False,False,False))
  if 'has_table_privilege' in sql:return Mock(fetchone=lambda:(self.writable,))
  return Mock(fetchall=lambda:[ACTIVE] if 'active_release a' in sql else [])

@pytest.fixture
def pg(monkeypatch,tmp_path):
 path=tmp_path/'synthetic.dsn';path.write_text('synthetic-placeholder')
 monkeypatch.setenv('RAG_BACKEND','postgres');monkeypatch.setenv('RAG_DSN_FILE',str(path))
 created=[]
 def connect(*args,**kwargs):
  assert kwargs['autocommit'] is True
  assert kwargs['options']=='-c default_transaction_read_only=on -c statement_timeout=3000'
  assert kwargs['connect_timeout']==5
  db=Connection();created.append(db);return db
 connector=Mock(side_effect=connect);monkeypatch.setattr(psycopg,'connect',connector)
 register=Mock();monkeypatch.setattr('pgvector.psycopg.register_vector',register)
 return connector,register,created,path

@pytest.mark.parametrize('operation',['refresh','rows','get','search'])
def test_closed_connection_recovers(pg,operation):
 connector,register,created,_=pg;s=store.Store();s.db.close()
 args={'refresh':(),'rows':(),'get':('c_'+'a'*64,),'search':('synthetic',)}
 getattr(s,operation)(*args[operation])
 assert connector.call_count==2 and register.call_count==2 and not s.db.closed
 assert all(any('rolsuper' in q for q in db.calls) and any('has_table_privilege' in q for q in db.calls) for db in created)

def test_inflight_disconnect_retried_once(pg):
 connector,register,created,_=pg;s=store.Store();old=s.db
 old.failure=psycopg.OperationalError('synthetic disconnect');s.refresh()
 assert old.closed and s.db is not old and connector.call_count==2

def test_disconnect_during_fetch_retried(pg,monkeypatch):
 _,_,_,_=pg;s=store.Store();old=s.db
 def fetchall():old.close();raise psycopg.OperationalError('synthetic fetch disconnect')
 monkeypatch.setattr(old,'execute',lambda *a:Mock(fetchall=fetchall))
 s.refresh();assert s.db is not old and old.closed

@pytest.mark.parametrize('privileged,writable',[(True,False),(False,True)])
def test_reconnect_rechecks_role_and_closes_candidate(pg,privileged,writable):
 connector,register,_,_=pg;s=store.Store();s.db.close()
 candidate=Connection(privileged,writable);connector.side_effect=None;connector.return_value=candidate
 with pytest.raises(RuntimeError,match='service DB role'):s.refresh()
 assert candidate.closed and s.db.closed and register.call_count==2

@pytest.mark.parametrize('error',[psycopg.OperationalError('synthetic unreachable'),psycopg.ProgrammingError('synthetic invalid DSN')])
def test_failed_reconnect_propagates_without_stale_fallback(pg,error):
 connector,_,_,_=pg;s=store.Store();s.db.close();connector.side_effect=error
 with pytest.raises(type(error)):s.refresh()
 assert connector.call_count==2 and s.db.closed

def test_missing_dsn_fails_on_reconnect(pg,monkeypatch):
 s=store.Store();s.db.close();monkeypatch.delenv('RAG_DSN_FILE')
 with pytest.raises(RuntimeError,match='requires secret'):s.refresh()

def test_registration_failure_closes_candidate(pg):
 connector,register,_,_=pg;s=store.Store();s.db.close()
 candidate=Connection();connector.side_effect=None;connector.return_value=candidate
 register.side_effect=RuntimeError('synthetic vector registration failure')
 with pytest.raises(RuntimeError,match='registration'):s.refresh()
 assert candidate.closed

def test_sql_timeout_is_not_retried(pg):
 connector,_,_,_=pg;s=store.Store();s.db.failure=psycopg.errors.QueryCanceled('synthetic timeout')
 with pytest.raises(psycopg.errors.QueryCanceled):s.refresh()
 assert connector.call_count==1

def test_second_disconnect_propagates(pg):
 connector,_,_,_=pg;s=store.Store();s.db.failure=psycopg.OperationalError('synthetic first disconnect')
 candidate=Connection();original=candidate.execute
 def execute(sql,params=None):
  if 'active_release a' in sql:candidate.close();raise psycopg.OperationalError('synthetic second disconnect')
  return original(sql,params)
 candidate.execute=execute;connector.side_effect=None;connector.return_value=candidate
 with pytest.raises(psycopg.OperationalError):s.refresh()
 assert connector.call_count==2

def test_concurrent_calls_reconnect_only_once(pg):
 connector,register,_,_=pg;s=store.Store();s.db.close();barrier=threading.Barrier(8)
 def work(_):barrier.wait();s.refresh()
 with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(work,range(8)))
 assert connector.call_count==2 and register.call_count==2
