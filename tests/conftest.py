"""Offline test guard installed before application modules are imported."""
import os,socket,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def deny(*args,**kwargs):raise AssertionError('offline tests prohibit network and process execution')
# These persist for this pytest process; tests never restore/relax the guard.
socket.socket.connect=deny
socket.socket.connect_ex=deny
socket.create_connection=deny
socket.getaddrinfo=deny
socket.socket.bind=deny
class DeniedPopen(subprocess.Popen):
 # Preserve Popen[bytes] annotations in SDK imports without allowing execution.
 def __init__(self,*args,**kwargs):deny()
subprocess.Popen=DeniedPopen
os.system=deny
for name in dir(os):
 if name.startswith(('exec','spawn','posix_spawn')):setattr(os,name,deny)
os.environ['RAG_ENABLE_QUERY_EMBEDDING']='0'
os.environ['RAG_BACKEND']='sqlite'
for name in ['RAG_OPENAI_KEY_FILE','RAG_DSN_FILE','RAG_HTTP_TOKEN_FILE']:
 os.environ.pop(name,None)
