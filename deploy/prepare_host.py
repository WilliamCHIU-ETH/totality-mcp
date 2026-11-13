#!/usr/bin/env python3
"""Host-only, explicitly executed setup. Generates NEW secrets, never prints them.
No existing credential is copied. Does not deploy or change networking.
"""
import os,pathlib,secrets
base=pathlib.Path(__file__).resolve().parent.parent
base.chmod(0o700)
d=base/'.secrets';d.mkdir(mode=0o700,exist_ok=True);d.chmod(0o700)
if any(d.iterdir()):raise SystemExit('Existing secrets present: refusing overwrite; use a deliberate rotation procedure.')
owner=secrets.token_urlsafe(36);reader=secrets.token_urlsafe(36);loader=secrets.token_urlsafe(36)
values={'pg_password':owner,'reader_password':reader,'owner_dsn':f'host=rag-db dbname=totality user=postgres password={owner}','reader_dsn':f'host=rag-db dbname=totality user=rag_runtime password={reader}','loader_password':loader,'loader_dsn':f'host=rag-db dbname=totality user=rag_ingest password={loader}','mcp_token':secrets.token_urlsafe(48)}
for name,value in values.items():
 p=d/name;p.write_text(value+'\n');p.chmod(0o444)
# Parent project remains 0700, protecting these files on host. Mounted secrets
# must be readable by distinct non-root container UIDs; only named services get them.
for p in (base/'data').rglob('*'):
 if p.is_dir():p.chmod(0o555)
 elif p.is_file():p.chmod(0o444)
(base/'data').chmod(0o555)
for p in [base/'schema.sql',base/'deploy/init-reader.sh']:p.chmod(0o444)
print('Generated deployment-only credentials under protected .secrets; values not displayed. Data is read-only. No services started.')
