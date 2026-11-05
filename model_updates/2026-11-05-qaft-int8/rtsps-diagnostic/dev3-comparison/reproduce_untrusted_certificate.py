import ssl,socket,subprocess,tempfile,threading,json,time,hashlib
from pathlib import Path
out=Path('/home/admin1/Documents/sentry_cv/model_updates/2026-11-05-qaft-int8/rtsps-diagnostic/dev3-comparison');out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='sentry-local-tls-repro-')as td:
 t=Path(td);cert=t/'server.crt';key=t/'server.key'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1','-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
 ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key)
 sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(16);sock.settimeout(.5);port=sock.getsockname()[1];stop=threading.Event();events=[]
 def handle(c):
  try:
   with ctx.wrap_socket(c,server_side=True)as s:
    events.append({'phase':'tls_accepted'});s.settimeout(3)
    while True:
     data=b''
     while b'\r\n\r\n'not in data:
      part=s.recv(4096)
      if not part:return
      data+=part
     lines=data.decode(errors='replace').splitlines();method=lines[0].split(' ',1)[0];events.append({'phase':'rtsp_request','method':method});seq=next((x.split(':',1)[1].strip()for x in lines if x.lower().startswith('cseq:')),'1')
     s.sendall(('RTSP/1.0 404 Not Found\r\nCSeq: '+seq+'\r\nContent-Length: 0\r\n\r\n').encode())
  except Exception as e:events.append({'phase':'connection_closed','type':type(e).__name__})
 def serve():
  while not stop.is_set():
   try:c,a=sock.accept()
   except socket.timeout:continue
   except OSError:return
   threading.Thread(target=handle,args=(c,),daemon=True).start()
 thread=threading.Thread(target=serve,daemon=True);thread.start();res=[]
 for name,image in [('dev3','ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.02-v8-dev3'),('qaft','localhost/sentry-v8-qaft-int8-model-candidate:2026.11.05')]:
  start=len(events);ffcmd=['podman','run','--rm','--network=host','--cpus=1','--entrypoint','ffmpeg',image,'-nostdin','-v','error','-rtsp_transport','tcp','-timeout','3000000','-i','rtsps://127.0.0.1:'+str(port)+'/synthetic-test','-t','1','-c','copy','-f','null','-'];ff=subprocess.run(ffcmd,capture_output=True,text=True,timeout=12);cmd=['podman','run','--rm','--network=host','--cpus=1','--pids-limit=128','--entrypoint','python3','-v','/tmp/sentry-rtsps-tls-check.py:/tmp/tls-check.py:ro',image,'/tmp/tls-check.py','rtsps://127.0.0.1:'+str(port)+'/synthetic-test']
  r=subprocess.run(cmd,capture_output=True,text=True,timeout=45);records=[json.loads(x)for x in r.stdout.splitlines()if x.startswith('{')];res.append({'image':name,'ffmpeg_transport':{'returncode':ff.returncode,'stderr':ff.stderr},'probe_returncode':r.returncode,'probe_records':records,'stderr':r.stderr,'server_events':events[start:]})
 stop.set();sock.close();thread.join(timeout=2)
 report={'scope':'Synthetic self-signed localhost TLS/RTSP server. Returns404 deliberately; reachingRTSP404 proves handshake completion, not video decoding. Private test key deleted with tempdir. No production TLS relaxation.','results':res}
 (out/'controlled-untrusted-certificate.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
