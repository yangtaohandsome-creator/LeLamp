"""Run from runtime root; Edge network/decoder check, ALSA null, no hardware."""
import asyncio,json,os,time,argparse
from pathlib import Path
from lelamp.voice.config import load_voice_config
load_voice_config()
os.environ['APLAY_DEVICE']='null'
os.environ['EDGE_TTS_DIAGNOSTICS']='1'
from lelamp.voice import edge_tts as m
parser=argparse.ArgumentParser()
parser.add_argument('--idle', type=float, nargs='+', default=[0,15,45,90])
parser.add_argument('--timeout', type=float, default=12)
parser.add_argument('--retry', action='store_true', help='Include the production retry path')
parser.add_argument('--output', default='runtime_state/edge_idle_results.json')
args=parser.parse_args()
results=[]
async def probe(age,text,label):
 c=m.EdgeSpeechClient.__new__(m.EdgeSpeechClient);c._websocket=None;c._session=None;c._connected_at=None;c._closed=False;c._connect_task=None
 c._trace=lambda stage,**fields:print(json.dumps({'case':label,'stage':stage,**fields},ensure_ascii=False),flush=True)
 try:
  ws=await asyncio.wait_for(c._connect(),10)
  await asyncio.sleep(age)
  before=time.monotonic()
  result=await asyncio.wait_for(c._speak(text,None,None) if args.retry else c._synthesize(ws,text,None,None),args.timeout)
  row={'case':label,'idle':age,'ok':True,'result':result,'elapsed':time.monotonic()-before}
 except Exception as e:row={'case':label,'idle':age,'ok':False,'error':type(e).__name__+': '+str(e)}
 finally:
  c._closed=True
  await c._close_async()
 results.append(row);print('RESULT '+json.dumps(row,ensure_ascii=False),flush=True)
async def main():
 for age in args.idle:
  await probe(age,'行，我睡了，有事再喊我。',f'short-{age}')
  await probe(0,'我能照亮桌面，也能帮你定闹钟、记待办和查天气。你想先试哪一个？',f'fresh-long-{age}')
 Path(args.output).write_text(json.dumps(results,ensure_ascii=False,indent=2))
asyncio.run(main())
