"""Resume at an immutable checkpoint, validate420 sources, then run the hour."""
from pathlib import Path
import datetime,json,os,signal,subprocess,sys,time,shlex,traceback
import campaign as c
r=c.ROOT;tools=c.REPO/'scripts/isolated_validation';prefix='a4-turbo-off-'
try:
 while True:
  state=json.loads((r/'CURRENT.json').read_text())
  if state.get('id')=='idle':break
  time.sleep(2)
 old=int((r/'extended.pid').read_text())
 try:os.kill(old,signal.SIGKILL)
 except ProcessLookupError:pass
 finished=state.get('finished_case')
 if finished and finished.startswith(prefix):
  config=json.loads((r/'artifacts'/finished/'case.json').read_text())
  c.run(finished.removeprefix(prefix),n=config['cameras'],fps=config['fps'],profile=config['profile'],fixture=config['fixture'],steady=config['steady_seconds'])
 out=r/'artifacts/marked-source-va-check';out.mkdir(exist_ok=True)
 (r/'CURRENT.json').write_text(json.dumps({'id':'marked-source-va-check','port':18080}))
 c.write(c.STATUS,{'phase':'hardware-validating-marked-sources','completed':len(c.RESULTS)})
 verdicts=json.loads((out/'results.json').read_text()) if (out/'results.json').exists() else []
 files=['latency-1080p30.mp4','latency-1080p30-420.mp4','latency-anpr720p10-420.mp4','latency-720p-420.mp4','latency-1440p-420.mp4']
 for name in files:
  if any(v['fixture']==name and v['passed']for v in verdicts):continue
  path=c.FIX/name;expected=('-420' in name)
  probe=subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=codec_name,profile,pix_fmt,width,height,avg_frame_rate','-of','json',str(path)],text=True)
  pipeline='gst-launch-1.0 -q filesrc location='+shlex.quote('/fixtures/'+name)+' ! qtdemux ! h264parse ! vah264dec ! vapostproc ! '+shlex.quote('video/x-raw(memory:VAMemory),format=NV12')+' ! fakesink num-buffers=10 sync=false'
  cmd=['docker','run','--rm','--device','/dev/dri','--group-add','992','--group-add','44','-v',str(c.FIX)+':/fixtures:ro','--entrypoint','bash',c.IMAGE,'-c',pipeline]
  with (out/(name+'.log')).open('w')as f:p=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=60)
  verdicts.append({'fixture':name,'ffprobe':json.loads(probe),'command':cmd,'expected_decode':expected,'returncode':p.returncode,'passed':(p.returncode==0)==expected})
 (out/'results.json').write_text(json.dumps(verdicts,indent=2))
 if not all(v['passed']for v in verdicts):raise RuntimeError('Marked source VA compatibility check failed; hour not started')
 case='marked-420-preflight-2x8';actual=prefix+case;epoch=r/'artifacts'/actual/'sender-epochs';epoch.mkdir(parents=True,exist_ok=True)
 pre=c.run(case,n=2,fixture='latency-1080p30-420.mp4',recordings=['latency-1080p30-420.mp4','latency-anpr720p10-420.mp4'],steady=60,env_override={'SENTRY_TEST_MARKER_REPLAY':str(epoch),'SENTRY_TEST_SCENE_GALLERY_LIMIT':'100'})
 if not pre.get('per_camera_rates')or pre.get('fatal_lines'):raise RuntimeError('Marked complete-pipeline preflight failed; preserving evidence before any hour retry')
 marker=r/'artifacts'/actual/'independent-latency-preflight.json'
 with marker.open('w')as f:
  relative=lambda p:'/campaign/'+str(p.relative_to(r))
  p=subprocess.run(['docker','run','--rm','--user','0','-v',str(r)+':/campaign:ro','-v',str(tools)+':/tools:ro','--entrypoint','python3',c.IMAGE,'/tools/analyze_markers.py',relative(epoch.parent/'gallery'),relative(epoch)],stdout=f,stderr=subprocess.STDOUT,timeout=120)
 if p.returncode:raise RuntimeError('Marker analysis preflight failed')
 measured=json.loads(marker.read_text())
 if measured['counts']['marker_decoded']<10 or measured['counts']['missing_sender_packet']:raise RuntimeError('Too few verified marker matches for an instrumented hour')
 count=2 if pre.get('candidate_passing_point') else 1
 held=c.run('stress-60min-all-'+str(count)+'x8-yuv420',n=count,recordings=['crowd-1080p.mp4'if i%2==0 else'ANPR.mp4'for i in range(count)],steady=3600,capacity=False)
 reportpath=r/'artifacts'/held['case']/'report.json'
 report=json.loads(reportpath.read_text())if reportpath.exists()else{}
 steady=[s for s in report.get('samples',[])if s.get('sample_phase')=='steady']
 if len(steady)<2 or steady[-1]['sample_monotonic_seconds']-steady[0]['sample_monotonic_seconds']<3580:raise RuntimeError('Hour observation window not completed; do not count startup or interrupted run')
 c.write(c.STATUS,{'phase':'initial-campaign-complete','completed':len(c.RESULTS),'hour_case':held['case'],'next':'resume remaining extended checks'})
 f=(r/'logs/extended-after-hour.log').open('w');child=subprocess.Popen(['python3',str(tools/'extended_campaign.py')],stdout=f,stderr=subprocess.STDOUT,start_new_session=True);(r/'extended.pid').write_text(str(child.pid));print('Extended resumed',child.pid,flush=True)
except Exception as e:
 traceback.print_exc();c.write(c.STATUS,{'phase':'priority-hour-error','error':type(e).__name__+': '+str(e),'completed':len(c.RESULTS)});raise
