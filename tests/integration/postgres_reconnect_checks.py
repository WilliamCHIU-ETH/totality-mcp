"""Reconnect checks in a disposable database, using synthetic rows only.

CI uses its ephemeral service. Private runs may supply an owner DSN *file* via
TOTALITY_TEST_OWNER_DSN_FILE. Never print the DSN. Only resources created by this
suite are removed; existing schemas, roles and databases are left untouched.
"""
import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

import build
from store import Store
from synthetic import CharacterTokenizer, make_source, add_vectors

class PostgresReconnectTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  owner_file=os.environ.get('TOTALITY_TEST_OWNER_DSN_FILE')
  if owner_file:
   cls.owner_dsn=Path(owner_file).read_text().strip()
  elif os.environ.get('TOTALITY_CI_PG_PORT'):
   cls.owner_dsn=make_conninfo(host='127.0.0.1',port=os.environ['TOTALITY_CI_PG_PORT'],dbname='totality_ci',user='postgres',password='ci-only-password',connect_timeout=5)
  else:raise unittest.SkipTest('requires isolated PostgreSQL owner connection')
  cls.name='reconnect_'+secrets.token_hex(8);cls.role=cls.name+'_reader'
  cls.directory=TemporaryDirectory();cls.addClassCleanup(cls.directory.cleanup)
  cls.owner=psycopg.connect(cls.owner_dsn,autocommit=True)
  cls.addClassCleanup(cls.owner.close)
  password=secrets.token_urlsafe(32)
  cls.owner.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(sql.Identifier(cls.role),sql.Literal(password)))
  cls.addClassCleanup(lambda:cls.owner.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(cls.role))))
  # Database must be dropped before its role; unittest cleanup runs LIFO.
  cls.owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
  cls.addClassCleanup(lambda:cls.owner.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(cls.name))))
  root=Path(cls.directory.name);data=root/'data'
  source=make_source(root/'source')
  with patch.object(build,'DATA',data),patch.object(build,'CONTEXT','context'),patch.object(build,'ENC',CharacterTokenizer()):
   build.build(source)
  chunks=add_vectors(data);cls.chunk=chunks[0]
  manifest=json.loads((data/'build-manifest.json').read_text());videos=json.loads((data/'videos.json').read_text())
  cls.release=manifest['release_id']
  db=psycopg.connect(make_conninfo(cls.owner_dsn,dbname=cls.name),autocommit=True)
  try:
   db.execute('CREATE EXTENSION vector; CREATE SCHEMA rag')
   db.execute('CREATE TABLE rag.releases(release_id text PRIMARY KEY,manifest jsonb,videos jsonb); CREATE TABLE rag.active_release(singleton boolean,release_id text); CREATE TABLE rag.chunks(chunk_id text,release_id text,video_id text,payload jsonb,keywords tsvector,embedding vector(1536))')
   # ACL-check targets exist but grant no execute to runtime.
   db.execute('CREATE FUNCTION rag.activate_release(text,text,boolean,boolean) RETURNS void LANGUAGE sql AS $$ SELECT NULL::void $$; CREATE FUNCTION rag.stage_release(jsonb,jsonb) RETURNS void LANGUAGE sql AS $$ SELECT NULL::void $$; REVOKE ALL ON ALL FUNCTIONS IN SCHEMA rag FROM PUBLIC')
   db.execute('INSERT INTO rag.releases VALUES (%s,%s,%s)',(cls.release,Jsonb(manifest),Jsonb(videos)))
   db.execute('INSERT INTO rag.active_release VALUES (true,%s)',(cls.release,))
   vectors=np.load(data/'vectors.npy')
   ids=json.loads((data/'vector-ids.json').read_text())
   for c in chunks:
    db.execute("INSERT INTO rag.chunks VALUES (%s,%s,%s,%s,to_tsvector('simple',%s),%s::vector)",(c['chunk_id'],cls.release,c['video_id'],Jsonb(c),c['text'],json.dumps(vectors[ids.index(c['chunk_id'])].tolist())))
   db.execute(sql.SQL('GRANT USAGE ON SCHEMA rag TO {}; GRANT SELECT ON ALL TABLES IN SCHEMA rag TO {}').format(sql.Identifier(cls.role),sql.Identifier(cls.role)))
  finally:db.close()
  cls.reader_file=root/'reader.dsn'
  cls.reader_file.write_text(make_conninfo(cls.owner_dsn,dbname=cls.name,user=cls.role,password=password,connect_timeout=5))
  cls.reader_file.chmod(0o600)

 def make_store(self):
  with patch.dict(os.environ,{'RAG_BACKEND':'postgres','RAG_DSN_FILE':str(self.reader_file),'RAG_ENABLE_QUERY_EMBEDDING':'0'}):
   s=Store()
  self.addCleanup(lambda:s.db.close())
  return s

 def test_closed_and_terminated_connections_recover(self):
  with patch.dict(os.environ,{'RAG_DSN_FILE':str(self.reader_file)}):
   s=self.make_store()
   for operation in ('refresh','rows','get','search','vector'):
    with self.subTest(operation=operation):
     old=s.db;pid=old.info.backend_pid
     self.assertTrue(self.owner.execute('SELECT pg_terminate_backend(%s)',(pid,)).fetchone()[0])
     if operation=='refresh':s.refresh();self.assertEqual(s.release,self.release)
     elif operation=='rows':self.assertEqual(len(s.rows()),4)
     elif operation=='get':self.assertEqual(s.get(self.chunk['chunk_id'])['chunk_id'],self.chunk['chunk_id'])
     elif operation=='search':self.assertTrue(s.search('synthetic001')['results'])
     else:self.assertTrue(s.search('synthetic001',query_vector=[1.0]+[0.0]*1535)['results'])
     self.assertTrue(old.closed);self.assertNotEqual(s.db.info.backend_pid,pid)
     self.assertEqual(s.db.execute('SHOW transaction_read_only').fetchone(),('on',))
     self.assertEqual(s.db.execute('SHOW statement_timeout').fetchone(),('3s',))
     self.assertTrue(s.db.autocommit)
   s.db.close();s.refresh();self.assertEqual(s.release,self.release)

 def test_reconnect_rejects_privileged_rotated_dsn(self):
  s=self.make_store();s.db.close()
  owner_file=Path(self.directory.name)/'owner.dsn'
  owner_file.write_text(make_conninfo(self.owner_dsn,dbname=self.name));owner_file.chmod(0o600)
  with patch.dict(os.environ,{'RAG_DSN_FILE':str(owner_file)}):
   with self.assertRaisesRegex(RuntimeError,'must not be privileged'):s.refresh()
  self.assertTrue(s.db.closed)

 def test_reconnect_rejects_new_publication_privilege(self):
  s=self.make_store();s.db.close()
  db=psycopg.connect(make_conninfo(self.owner_dsn,dbname=self.name),autocommit=True)
  try:
   db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION rag.stage_release(jsonb,jsonb) TO {}').format(sql.Identifier(self.role)))
   with patch.dict(os.environ,{'RAG_DSN_FILE':str(self.reader_file)}):
    with self.assertRaisesRegex(RuntimeError,'write or publication privilege'):s.refresh()
   self.assertTrue(s.db.closed)
  finally:
   db.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION rag.stage_release(jsonb,jsonb) FROM {}').format(sql.Identifier(self.role)))
   db.close()

if __name__=='__main__':unittest.main()
