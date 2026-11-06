#!/usr/bin/env python3
"""Bounded image recovery tests; live source, temporary TLS relay, V8 API capture.

This checks native execution, source isolation and package integrity at /v1/packages.
It does not test recognition accuracy, remote Management ingest or maximum capacity.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[2]
UPSTREAM='rtsps://gpu-box.burmilla-bass.ts.net:443/traffic1'

class Relay:
    def __init__(self,temporary):
        cert=Path(temporary)/'server.crt';key=Path(temporary)/'server.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
        self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.context.load_cert_chain(cert,key)
        self.upstream_context=ssl.create_default_context()
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen(16);self.listener.settimeout(.2)
        self.url=f'rtsps://127.0.0.1:{self.listener.getsockname()[1]}/traffic1'
        self.stop=threading.Event();self.block=threading.Event();self.connections=[];self.lock=threading.Lock();self.accepted=0
        threading.Thread(target=self.accept,daemon=True).start()
    def accept(self):
        while not self.stop.is_set():
            try:s,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:return
            if self.block.is_set():s.close();continue
            threading.Thread(target=self.forward,args=(s,),daemon=True).start()
    def forward(self,plain):
        pair=[]
        try:
            plain.settimeout(8)
            client=self.context.wrap_socket(plain,server_side=True);pair.append(client)
            raw=socket.create_connection(('gpu-box.burmilla-bass.ts.net',443),timeout=8)
            upstream=self.upstream_context.wrap_socket(raw,server_hostname='gpu-box.burmilla-bass.ts.net');pair.append(upstream)
            with self.lock:self.connections.extend(pair);self.accepted+=1
            for s in pair:s.settimeout(3)
            while not self.stop.is_set() and not self.block.is_set():
                ready=[s for s in pair if s.pending()]
                if not ready:ready,_,_=select.select(pair,[],[],.2)
                for s in ready:
                    data=s.recv(65536)
                    if not data:return
                    (upstream if s is client else client).sendall(data)
        except (OSError,ssl.SSLError):pass
        finally:
            with self.lock:
                for s in pair:
                    if s in self.connections:self.connections.remove(s)
                    try:s.close()
                    except OSError:pass
    def interrupt(self):
        self.block.set()
        with self.lock:
            for s in self.connections:
                try:s.shutdown(socket.SHUT_RDWR)
                except OSError:pass
    def close(self):
        self.stop.set();self.interrupt();self.listener.close()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--image',required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--mode',choices=['reconnect','native-hang'],default='reconnect');ap.add_argument('--port',type=int,default=18381)
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);out.chmod(0o755)
    config=out/'config';secrets=out/'secrets';delivery=Path(tempfile.mkdtemp(prefix='sv8-rec-'));fault=out/'fault'
    for p in (config,secrets,delivery,fault):p.mkdir(exist_ok=True);p.chmod(0o755)
    delivery.chmod(0o777)
    spec=importlib.util.spec_from_file_location('capacity_helper',ROOT/'scripts/v8_capacity_probe.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    report={'image':args.image,'mode':args.mode,'scope':__doc__,'passed':False,'phases':[]}
    samples=[];agent=None;name=f'sentry-recovery-{args.mode}-{os.getpid()}'
    log_handle=(out/'agent.log').open('w')
    def health():
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/healthz',timeout=5) as r:h=json.load(r)
            row={'monotonic':time.monotonic(),'wall_time':time.time(),'health':h};samples.append(row)
            with (out/'health.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
            return h
        except Exception:return {}
    def cameras(h):return {c['camera_id']:c for c in h.get('telemetry',{}).get('cameras',[])}
    def decoded(c):return c.get('processing',{}).get('totals',{}).get('decoded_frames',0)
    def healthy(c):return c.get('decoder',{}).get('state')=='healthy' and (c.get('decoder',{}).get('last_frame_age_seconds') or 0)<10
    def verified_entries():
        r=subprocess.run(['podman','logs',name],capture_output=True,text=True,timeout=10)
        entries=[]
        for line in (r.stdout+r.stderr).splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('event')=='camera_recovery_verified' and event.get('camera_id')=='capacity-01':entries.append(event)
        return entries
    def verified_recoveries():return len(verified_entries())
    def wait_for(predicate,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            h=health();cs=cameras(h)
            if predicate(h,cs):return h,cs
            time.sleep(2)
        raise RuntimeError('bounded recovery condition not satisfied')
    try:
        with tempfile.TemporaryDirectory(prefix='sentry-recovery-certs-') as tmp:
            relay=Relay(tmp)
            try:
                state=helper.desired_state(2,4,full_usecases=True)
                (config/'desired_state.json').write_text(json.dumps(state))
                for cam,url in zip(state['cameras'],[relay.url,UPSTREAM]):
                    (secrets/(cam['camera_id']+'.url')).write_text(url+'\n')
                agent=subprocess.Popen([sys.executable,str(Path(__file__).with_name('v8_capture_agent.py')),'--socket',str(delivery/'agent.sock'),'--schemas',str(ROOT/'newdetails/sentry-v8'),'--log',str(out/'packages.jsonl'),'--status',str(out/'agent-status.json')],stdout=log_handle,stderr=subprocess.STDOUT)
                deadline=time.monotonic()+10
                while not (delivery/'agent.sock').exists() and time.monotonic()<deadline:time.sleep(.1)
                if not (delivery/'agent.sock').exists():raise RuntimeError('agent capture startup failed')
                cmd=['podman','run','-d','--name',name,'--network=host','--device','/dev/dri','--group-add','keep-groups','-e',f'APEX_API_PORT={args.port}','-e','SENTINEL_DLS_PROGRESS_INTERVAL_SECONDS=5','-e','SENTINEL_SURFACE_FIRST_FRAME_TIMEOUT_SECONDS=15','-v',f'{config}/desired_state.json:/configs/desired_state.json:ro','-v',f'{secrets}:/run/secrets/sentinel/cameras:ro','-v',f'{delivery}:/run/sentinel-delivery']
                if Path('/dev/accel/accel0').exists():cmd+=['--device','/dev/accel/accel0']
                if args.mode=='native-hang':
                    (fault/'sitecustomize.py').write_text('''import sys,os,time\nsys.path.insert(0,"/opt/traffic-pilot/services/worker")\nfrom pipeline.multi_camera_gst import MultiCameraGstPipeline\noriginal=MultiCameraGstPipeline.create_branch\ndef create(self,camera_id,description,configure=None):\n    def setup(branch):\n        if configure:configure(branch)\n        if camera_id!="capacity-01":return\n        from gi.repository import Gst\n        count=[0]\n        def stall(pad,info):\n            count[0]+=1\n            if count[0]==40:\n                try:fd=os.open("/tmp/sentry-native-fault-once",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)\n                except FileExistsError:return Gst.PadProbeReturn.OK\n                import logging\n                logging.getLogger("test_fault").error("test injected blocked primary callback",extra={"event":"test_injected_native_stall","camera_id":camera_id})\n                while True:time.sleep(1)\n            return Gst.PadProbeReturn.OK\n        branch.get_by_name("primarydet").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,stall)\n    return original(self,camera_id,description,configure=setup)\nMultiCameraGstPipeline.create_branch=create\n''')
                    inspect=json.loads(subprocess.check_output(['podman','image','inspect',args.image],text=True))[0]
                    existing_path=next((e.split('=',1)[1] for e in inspect['Config'].get('Env',[]) if e.startswith('PYTHONPATH=')),'/opt/traffic-pilot')
                    cmd+=['-e','PYTHONPATH=/fault:'+existing_path,'-v',f'{fault}:/fault:ro']
                cmd.append(args.image);(out/'command.json').write_text(json.dumps(cmd,indent=2))
                subprocess.run(cmd,check=True,capture_output=True,timeout=40)
                print('image started; waiting for two active cameras',flush=True)
                h,cs=wait_for(lambda h,cs:len(cs)==2 and all(healthy(c) and decoded(c)>=5 for c in cs.values()) and h.get('ready'),150)
                initial_pid=cs['capacity-01'].get('pid');report['initial_worker_pid']=initial_pid
                report['phases'].append({'phase':'baseline','counts':{k:decoded(v) for k,v in cs.items()}})
                if args.mode=='reconnect':
                    for round_no in (1,2):
                        before={k:decoded(v) for k,v in cs.items()};pid=cs['capacity-02'].get('pid');verified_before=verified_recoveries()
                        relay.interrupt();print(f'round {round_no}: relay offline for 18 seconds',flush=True)
                        end=time.monotonic()+18;peer_states=[]
                        while time.monotonic()<end:
                            h=health();cs=cameras(h);peer=cs.get('capacity-02',{})
                            peer_states.append({'healthy':healthy(peer),'pid':peer.get('pid'),'decoded':decoded(peer),'ready':h.get('ready')})
                            time.sleep(2)
                        relay.block.clear();print(f'round {round_no}: relay restored; waiting for fresh frames',flush=True)
                        h,cs=wait_for(lambda h,cs:len(cs)==2 and healthy(cs['capacity-01']) and decoded(cs['capacity-01'])>before['capacity-01']+5 and verified_recoveries()>verified_before,90)
                        entries=verified_entries()
                        session=cs['capacity-01'].get('decoder',{}).get('stream_session_id')
                        assert entries[-1].get('stream_session_id')==session,'health/log stream session mismatch'
                        result={'round':round_no,'recovered_stream_session_id':session,'verified_recovery_log_count':verified_recoveries(),'peer_kept_processing':bool(peer_states and all(s['healthy'] and s['ready'] and s['pid']==pid for s in peer_states) and peer_states[-1]['decoded']>before['capacity-02']), 'worker_pid_preserved':cs['capacity-01'].get('pid')==initial_pid,'fresh_counts':{k:decoded(v) for k,v in cs.items()},'peer_samples':peer_states}
                        report['phases'].append(result)
                        assert result['peer_kept_processing'] and result['worker_pid_preserved'], 'source outage disrupted healthy peer/group'
                        time.sleep(5)
                else:
                    print('waiting for one-shot injected native stall and supervised replacement',flush=True)
                    h,cs=wait_for(lambda h,cs:len(cs)==2 and all(healthy(c) and decoded(c)>=8 for c in cs.values()) and cs['capacity-01'].get('pid')!=initial_pid and h.get('ready'),150)
                    report['replacement_worker_pid']=cs['capacity-01'].get('pid')
                    report['phases'].append({'phase':'replacement_recovered','counts':{k:decoded(v) for k,v in cs.items()}})
                    time.sleep(8);h=health()
                report['final_health']=h
                packages=[]
                for line in (out/'packages.jsonl').read_text().splitlines():
                    try:packages.append(json.loads(line))
                    except ValueError:pass
                session_by_camera={k:v.get('decoder',{}).get('stream_session_id') for k,v in cameras(h).items()}
                report['package_session_matches']={k:any(p.get('camera_id')==k and p.get('stream_session_id')==sid for p in packages) for k,sid in session_by_camera.items()}
                assert all(report['package_session_matches'].values()),'package session does not match current health'
                report['relay_tls_connections']=relay.accepted
                report['agent']=json.loads((out/'agent-status.json').read_text())
                assert report['agent'].get('evidence_bytes',0)>0 and report['agent'].get('embeddings',0)>0
                assert set(report['agent'].get('cameras',{}))=={'capacity-01','capacity-02'}
                report['passed']=True
            finally:relay.close()
    except Exception as e:
        report['error']=str(e);print('FAILED:',type(e).__name__,str(e),flush=True)
    finally:
        logs=subprocess.run(['podman','logs',name],capture_output=True,text=True,timeout=20)
        text=logs.stdout+logs.stderr;(out/'container.log').write_text(text)
        events=[]
        for line in text.splitlines():
            try:d=json.loads(line)
            except ValueError:continue
            if d.get('event') in ['camera_pipeline_stalled','camera_source_restart_begin','camera_source_restart_requested','camera_recovery_verified','native_graph_rebuild_requested','native_graph_watchdog_fenced','native_worker_watchdog_restart','camera_process_restarted','rtsp_video_pad_linked']:events.append(d)
        report['recovery_events']=events
        if report['passed']:
            expected='camera_recovery_verified' if args.mode=='reconnect' else 'native_worker_watchdog_restart'
            if not any(d['event']==expected for d in events):report['passed']=False;report['error']='required recovery log missing'
        subprocess.run(['podman','rm','-f',name],capture_output=True,timeout=20)
        if agent is not None:
            agent.terminate()
            try:agent.wait(timeout=5)
            except subprocess.TimeoutExpired:agent.kill();agent.wait()
        log_handle.close()
        shutil.rmtree(delivery,ignore_errors=True)
        (out/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'passed':report['passed'],'error':report.get('error'),'report':str(out/'report.json'),'recovery_events':len(events)},indent=2),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
