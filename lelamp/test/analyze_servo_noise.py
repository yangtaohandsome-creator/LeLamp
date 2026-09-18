"""Offline servo spectrum/template feasibility report."""
from __future__ import annotations
import argparse, csv, json, math, wave
from collections import defaultdict
from pathlib import Path
import numpy as np
from ..voice.config import load_voice_config
from ..voice.vad import make_vad

def read_stereo(path):
    with wave.open(str(path), "rb") as w:
        rate=w.getframerate(); ch=w.getnchannels(); x=np.frombuffer(w.readframes(w.getnframes()),dtype="<i2")
    return x.reshape(-1,ch).astype(np.float32)/32768, rate

def spectrum(x, rate):
    size=2048; hop=1024
    if len(x)<size: x=np.pad(x,(0,size-len(x)))
    frames=np.stack([x[i:i+size] for i in range(0,max(1,len(x)-size+1),hop)])
    p=np.abs(np.fft.rfft(frames*np.hanning(size),axis=1))**2
    return np.median(p,axis=0)+1e-12, np.fft.rfftfreq(size,1/rate)

def vad_trigger(x, rate):
    d=make_vad(); target=16000
    y=np.interp(np.arange(round(len(x)*target/rate))*rate/target,np.arange(len(x)),x).astype(np.float32)
    active=0
    for i in range(0,len(y),1600):
        b=y[i:i+1600]
        if len(b)<1600:b=np.pad(b,(0,1600-len(b)))
        d.accept_waveform(b); active += int(d.is_speech_detected())
    return active

def notch_fft(x, rate, frequencies, width=35):
    if not frequencies:return x.copy()
    n=len(x); f=np.fft.rfftfreq(n,1/rate); z=np.fft.rfft(x)
    mask=np.ones(len(f))
    for hz in frequencies: mask[np.abs(f-hz)<=width]=0
    return np.fft.irfft(z*mask,n).astype(np.float32)

def svg_plot(path, series, max_hz=8000):
    width,height=1000,500; colors=["#e74c3c","#3498db","#2ecc71","#9b59b6","#f39c12"]
    chunks=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="100%" height="100%" fill="white"/>']
    for idx,(name,freq,db) in enumerate(series[:5]):
        keep=freq<=max_hz; xx=40+(freq[keep]/max_hz)*(width-60); yy=20+(1-np.clip((db[keep]+10)/50,0,1))*(height-50)
        points=" ".join(f"{x:.1f},{y:.1f}" for x,y in zip(xx[::4],yy[::4]))
        chunks.append(f'<polyline fill="none" stroke="{colors[idx%len(colors)]}" points="{points}"/><text x="50" y="{35+idx*18}" fill="{colors[idx%len(colors)]}">{name}</text>')
    chunks.append('</svg>'); path.write_text("".join(chunks))

def main():
    p=argparse.ArgumentParser(); p.add_argument("directory",type=Path); args=p.parse_args(); load_voice_config()
    meta=json.loads((args.directory/"markers.json").read_text()); audio,rate=read_stereo(args.directory/meta["wav"])
    baselines=[m for m in meta["markers"] if m["category"]=="baseline"]
    base=np.concatenate([audio[int(m["start"]*rate):int(m["end"]*rate)] for m in baselines],axis=0)
    base_psd,freq=spectrum(base.mean(axis=1),rate)
    grouped=defaultdict(list); rows=[]; plots=[]
    for m in meta["markers"]:
        if m["category"] in {"baseline","restore","safety"} or m["status"]!="completed":continue
        seg=audio[int(m["start"]*rate):int(m["end"]*rate)]
        mono=seg.mean(axis=1); psd,_=spectrum(mono,rate); excess=10*np.log10(psd/base_psd)
        candidates=[]
        for i in range(2,len(freq)-2):
            if 80<=freq[i]<=8000 and excess[i]>=10 and excess[i]==max(excess[i-2:i+3]): candidates.append((float(excess[i]),float(freq[i])))
        candidates=sorted(candidates,reverse=True)[:8]
        grouped[m["category"]].append({"freqs":[v for _,v in candidates],"segment":mono})
        rows.append({"name":m["name"],"category":m["category"],"duration":m["end"]-m["start"],
                     "rms_ch0":float(np.std(seg[:,0])),"rms_ch1":float(np.std(seg[:,1])),
                     "vad_blocks":vad_trigger(mono,rate),"peaks_hz":";".join(f"{v:.1f}" for _,v in candidates)})
        plots.append((m["name"],freq,excess))
    stable=[]
    for category,reps in grouped.items():
        for hz in reps[0]["freqs"]:
            hits=sum(any(abs(x-hz)<=60 for x in r["freqs"]) for r in reps)
            if hits>=math.ceil(len(reps)*.67):stable.append((category,hz,hits,len(reps)))
    # Cross-category candidates must appear in at least 70% of motion classes.
    bins=defaultdict(set)
    for category,hz,_,_ in stable: bins[round(hz/60)*60].add(category)
    needed=max(2,math.ceil(len(grouped)*.70)); filters=sorted(float(hz) for hz,cats in bins.items() if len(cats)>=needed)
    before=after=0
    for reps in grouped.values():
        for r in reps:
            before += int(vad_trigger(r["segment"],rate)>0)
            after += int(vad_trigger(notch_fft(r["segment"],rate,filters),rate)>0)
    feasible=bool(filters) and after==0
    report={"movement_categories":len(grouped),"segments":len(rows),"stable_by_category":stable,
            "cross_action_notches_hz":filters,"vad_false_segments_before":before,
            "vad_false_segments_after":after,"fixed_notch_feasible":feasible,
            "decision":"candidate_for_human_validation" if feasible else "fixed_frequency_filter_not_sufficient"}
    (args.directory/"analysis.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    with (args.directory/"segments.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
    svg_plot(args.directory/"spectra.svg",plots)
    print(json.dumps(report,ensure_ascii=False,indent=2)); print(f"ANALYSIS SAVED: {args.directory.resolve()}")

if __name__=="__main__":main()
