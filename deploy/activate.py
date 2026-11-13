#!/usr/bin/env python3
"""CAS release switch/rollback; no DDL or metadata file mutation."""
import argparse,pathlib,re,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent.parent))
import psycopg
from import_pg import activate_release

def main():
 p=argparse.ArgumentParser();p.add_argument('--dsn-file',required=True);p.add_argument('--release',required=True)
 p.add_argument('--expected-base-release');p.add_argument('--allow-initial',action='store_true')
 p.add_argument('--rollback',action='store_true',help='Explicitly switch to a retained release; still requires CAS')
 a=p.parse_args()
 if any(not re.fullmatch(r'r1-[a-f0-9]{20}',r) for r in [a.release,a.expected_base_release] if r is not None):raise SystemExit('invalid release')
 if a.expected_base_release is None and not a.allow_initial:raise SystemExit('expected base required, or allow_initial for empty database')
 with psycopg.connect(pathlib.Path(a.dsn_file).read_text().strip()) as db:
  activate_release(db,a.release,a.expected_base_release,a.allow_initial,a.rollback)
 print('Active release switched atomically; matching DB metadata is refreshed on the next read. Old release retained.')
if __name__=='__main__':main()
