#!/usr/bin/env python3
"""Summarize same-frame monotonic timestamps; excludes first 10s warmup.
read() duration includes waiting/decode, NOT exposure-to-host latency.
Display submission is NOT physical screen presentation; writes are NOT motion.
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import statistics


def analyze(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    start = min(r['t'] for r in rows if 't' in r) + 10
    rows = [r for r in rows if r.get('t', start) >= start]
    frames = {r['seq']: r for r in rows if r['type'] == 'perception'}
    captures = {r['seq']: r for r in rows if r['type'] == 'capture'}
    by_time = {r['captured_at']: r for r in frames.values()}
    metrics = defaultdict(list)
    def add(name, value):
        metrics[name].append(value * 1000)
    for r in frames.values():
        for name, a, b in [
            ('frame_wait', 'captured_at', 'started'), ('resize', 'started', 'resized'),
            ('yunet', 'resized', 'face_done'), ('face_association_rgb', 'face_done', 'hands_started'),
            ('hands_total', 'hands_started', 'hands_done'),
            ('hand_association_gestures', 'hands_done', 'completed'),
            ('publish', 'completed', 'published'), ('inference_total', 'started', 'published'),
            ('capture_to_result', 'captured_at', 'published')]:
            add(name, r[b] - r[a])
        add('inference_with_hands' if r['hands'] else 'inference_no_hands', r['published']-r['started'])
        if r['seq'] in captures:
            c = captures[r['seq']]
            add('read_wait_decode', c['captured_at']-c['read_started'])
            add('rotate_publish', c['published']-c['captured_at'])
    seen = set()
    matched = unmatched = 0
    for r in rows:
        if r['type'] == 'hands_detail':
            add('mediapipe_official', r['official_done']-r['official_started'])
            if r['hands']:
                add('aligned_branch', r['aligned_seconds'])
                add('roll_z_branch', r['roll_seconds'])
                add('mediapipe_with_hands', r['official_done']-r['official_started'])
        if r['type'] == 'cycle':
            add('control_interval', r['interval'])
            if 'write_ms' in r:
                metrics['serial_write'].append(r['write_ms'])
            if 'write_done' in r and 'target_at' in r:
                p = by_time.get(r['target_at'])
                if p is None:
                    unmatched += 1
                    continue
                matched += 1
                add('all_control_target_age', r['write_done']-r['target_at'])
                if p['seq'] not in seen:
                    seen.add(p['seq'])
                    add('first_control_result_wait', r['cycle_start']-p['published'])
                    add('capture_to_first_write', r['write_done']-p['captured_at'])
                else:
                    add('reused_control_target_age', r['write_done']-p['captured_at'])
        if r['type'] == 'display' and r['seq'] in frames:
            p=frames[r['seq']]
            for name, value in [
                ('result_to_enqueue', r['enqueued']-p['published']),
                ('display_ipc_wait', r['received']-r['enqueued']),
                ('display_draw', r['rendered']-r['received']),
                ('display_resize_submit_events', r['submitted']-r['rendered']),
                ('result_to_display_submit', r['submitted']-p['published']),
                ('capture_to_display_submit', r['submitted']-p['captured_at'])]:
                add(name, value)
    def summary(values):
        s=sorted(values)
        return dict(n=len(s), p50=round(statistics.median(s),2),
                    p95=round(s[min(len(s)-1,int(.95*len(s)))],2),
                    mean=round(statistics.mean(s),2), maximum=round(max(s),2))
    return dict(file=str(path), warmup_seconds=10, units='ms',
                frames=len(frames), scene_counts=dict(Counter(f"faces={r['faces']},hands={r['hands']}" for r in frames.values())),
                control_matched=matched, control_unmatched=unmatched,
                writer=[r for r in rows if r['type']=='writer_summary'],
                metrics={k:summary(v) for k,v in metrics.items()})

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path',type=Path)
    print(json.dumps(analyze(parser.parse_args().path), indent=2))
