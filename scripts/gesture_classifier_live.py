#!/usr/bin/env python3
"""Read-only camera comparison; never creates LampApp or a motion controller."""
import argparse
import json
import signal
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
from lelamp.vision.camera import LatestFrameSource
from lelamp.vision.config import load_vision_config
from lelamp.vision.hands import MediaPipeHands
from lelamp.vision.gesture_classifier_experiment import (
    BUNDLE_SHA256, LABELS, image_roll_inputs, BundledGestureClassifier, palm_aligned_inputs, preprocess_landmarks,
)
from lelamp.vision.gesture_fusion_experiment import fuse_gestures
from lelamp.vision.hand_target import HandTargetManager
from vision_preview import HAND_CONNECTIONS, disable_display_blanking, fit_screen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=0, help='0 runs until stopped')
    parser.add_argument('--no-preview', action='store_true', help='SSH only, no display required')
    parser.add_argument('--windowed', action='store_true')
    parser.add_argument('--capture-dir', type=Path, help='Labeled landmark capture: keys 1/2/3, 0 pauses')
    parser.add_argument('--terminal-capture', action='store_true', help='SSH terminal: number + Enter, 3s preparation, 10s capture')
    args = parser.parse_args()
    if args.terminal_capture and not args.capture_dir:
        parser.error('--terminal-capture requires --capture-dir')
    config = load_vision_config()
    classifier = BundledGestureClassifier(config.gesture_model)
    hands = MediaPipeHands(config, fused=False)
    source = LatestFrameSource(config)
    targets = HandTargetManager(config)
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = last_report = time.monotonic()
    last_sequence = -1
    frames = 0
    hz = 0.0
    capture = None
    capture_label = None
    capture_frames = 0
    pending_label = None
    capture_start = capture_end = 0.0
    poses = {'1': ('palm_upright_open', '掌心朝镜头，手指朝上，五指张开'),
             '2': ('back_upright_open', '手背朝镜头，手指朝上，五指张开'),
             '3': ('back_inverted_open', '手背朝镜头，手指朝下，五指张开'),
             '4': ('palm_upright_fist', '掌心侧朝镜头，正立握拳'),
             '5': ('back_upright_fist', '手背朝镜头，正立握拳'),
             '6': ('back_inverted_fist', '手背朝镜头，倒立握拳'),
             '7': ('relaxed_negative', '手指自然半弯，既不完全张开也不握拳，缓慢转动手腕'),
             '8': ('victory', '保持剪刀手，缓慢转动手腕'),
             '9': ('thumb_up', '保持竖拇指，缓慢转动手腕')}
    expected_by_label = {label: ('Open_Palm' if label.endswith('_open') else
        'Closed_Fist' if label.endswith('_fist') else {'relaxed_negative': None,
        'victory': 'Victory', 'thumb_up': 'Thumb_Up'}[label]) for label, _ in poses.values()}
    menu = '\n输入数字后回车：1=掌心正立  2=手背正立  3=手背倒立\n4=掌心握拳  5=手背正立握拳  6=手背倒立握拳\n7=半弯非目标姿态  8=剪刀手  9=竖拇指  0=暂停  q=保存退出\n每组准备3秒，采集10秒后自动暂停；可任意选择、重复采集。'
    try:
        if args.capture_dir:
            args.capture_dir.mkdir(parents=True, exist_ok=True)
            capture = (args.capture_dir / f'landmarks-{time.time_ns()}.jsonl').open('x')
            capture.write(json.dumps(dict(type='metadata', model_sha256=BUNDLE_SHA256,
                labels=LABELS, model_size=config.model_size, alignment='legacy-screen-x', diagnostics=['image_roll','image_roll_z']))+'\n')
            capture.flush()
        source.start()
        if source.wait_for_first_frame(10) is None:
            raise RuntimeError(str(source.health()))
        if not args.no_preview:
            cv2.namedWindow('Gesture comparison', cv2.WINDOW_NORMAL)
            if not args.windowed:
                cv2.resizeWindow('Gesture comparison', 1920, 1080)
                cv2.moveWindow('Gesture comparison', 0, 0)
                cv2.setWindowProperty('Gesture comparison', cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            disable_display_blanking()
        print('READY: Original=official; Aligned=experimental; Q/Esc quits; no motor control', flush=True)
        if args.terminal_capture:
            print(f'数据文件：{capture.name}\n请使用同一只手、相近距离；按每组选项摆姿态，允许轻微改变角度。{menu}', flush=True)
        while not stopped and (not args.seconds or time.monotonic()-started < args.seconds):
            cycle = time.monotonic()
            if args.terminal_capture:
                if select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline()
                    command = line.strip().lower()
                    if not line or command == 'q':
                        break
                    if command == '0' or command in poses:
                        capture_label = pending_label = None
                        if command == '0':
                            print('已暂停。'+menu, flush=True)
                        else:
                            pending_label, instruction = poses[command]
                            capture_start = cycle + 3.0
                            capture_end = capture_start + 10.0
                            capture_frames = 0
                            print(f'准备3秒：{instruction}。随后自动开始采集10秒。', flush=True)
                    else:
                        print('无效输入。'+menu, flush=True)
                if pending_label and cycle >= capture_start:
                    capture_label, pending_label = pending_label, None
                    print('开始采集，请保持姿态。', flush=True)
                if capture_label and cycle >= capture_end:
                    print(f'本组完成：{capture_label}，已记录{capture_frames}帧（含未检测到手的帧）。'+menu, flush=True)
                    capture_label = None
            packet = source.latest()
            if packet is None or packet.sequence == last_sequence:
                if source.health()['status'] == 'error':
                    raise RuntimeError(str(source.health()))
                time.sleep(.01)
                continue
            if cycle-packet.captured_at > 2:
                raise RuntimeError('Camera frame is stale')
            last_sequence = packet.sequence
            rgb = cv2.cvtColor(cv2.resize(packet.image, config.model_size), cv2.COLOR_BGR2RGB)
            observations = hands.recognize(rgb, int(packet.captured_at*1000))
            observations = targets.update(observations, packet.captured_at).hands
            canvas = cv2.resize(packet.image, (960, 720))
            rows = []
            samples = []
            for i, hand in enumerate(observations):
                try:
                    baseline = classifier.classify(preprocess_landmarks(hand.landmarks_normalized_3d, config.model_size), preprocess_landmarks(hand.world_landmarks), .5)
                    aligned = classifier.classify(*palm_aligned_inputs(hand.landmarks_normalized_3d, hand.world_landmarks), .5)
                    diagnostic = {}
                    for name, flip in [('image_roll', False), ('image_roll_z', True)]:
                        try:
                            inputs = image_roll_inputs(hand.landmarks_normalized_3d, config.model_size, flip_z=flip)
                            prediction = classifier.classify(inputs, preprocess_landmarks(hand.world_landmarks), .5)
                            diagnostic[name] = dict(label=prediction.label, confidence=prediction.confidence,
                                scores=prediction.scores, input=inputs.tolist(), elapsed_ms=prediction.elapsed_ms)
                        except ValueError as exc:
                            diagnostic[name] = dict(error=str(exc))
                    candidate = diagnostic['image_roll_z']
                    merged = fuse_gestures((hand.gesture, hand.gesture_confidence),
                        (aligned.label, aligned.confidence), (candidate.get('label','None'), candidate.get('confidence',0)))
                    fusion_info = dict(raw=merged.label, label=merged.label, labels=merged.labels, reason=merged.reason, source=merged.source)
                    if capture is not None and capture_label is not None:
                        samples.append(dict(hand=i, side=hand.handedness, diagnostics=diagnostic, fusion=fusion_info, track_id=hand.track_id,
                            image_xyz=hand.landmarks_normalized_3d, world_xyz=hand.world_landmarks,
                            original_label=hand.gesture, original_score=hand.gesture_confidence,
                            baseline_input=preprocess_landmarks(hand.landmarks_normalized_3d, config.model_size).tolist(),
                            aligned_input=palm_aligned_inputs(hand.landmarks_normalized_3d, hand.world_landmarks)[0].tolist(),
                            baseline_scores=baseline.scores, aligned_scores=aligned.scores,
                            screen_x_mirror=hand.landmarks_normalized_3d[17][0] < hand.landmarks_normalized_3d[5][0]))
                    candidate = diagnostic['image_roll_z']
                    matched = baseline.label == hand.gesture and abs(baseline.confidence-hand.gesture_confidence) < 1e-4
                    row = dict(hand=i, side=hand.handedness, fusion=fusion_info, candidate=candidate.get('label','INVALID'), candidate_score=candidate.get('confidence',0), original=hand.gesture, original_score=round(hand.gesture_confidence,4), aligned=aligned.label, aligned_score=round(aligned.confidence,4), baseline_match=matched, classifier_ms=round(aligned.elapsed_ms,3))
                except ValueError as exc:
                    row = dict(hand=i, error=str(exc))
                    samples.append(row)
                rows.append(row)
                pts = [(int(x*960),int(y*720)) for x,y in hand.landmarks_normalized]
                for a,b in HAND_CONNECTIONS:
                    cv2.line(canvas,pts[a],pts[b],(0,255,0),2)
                for p in pts:
                    cv2.circle(canvas,p,4,(255,0,255),-1)
                texts = [f'Hand {i+1} {hand.handedness} Original: {hand.gesture} {hand.gesture_confidence:.2f}',
                         f"Aligned: {row.get('aligned','INVALID')} {row.get('aligned_score',0):.2f}  baseline_match={row.get('baseline_match',False)}",
                         f"Roll+Z (test): {row.get('candidate','INVALID')} {row.get('candidate_score',0):.2f}",
                         f"Fused: {row.get('fusion',{}).get('label','None')}  {row.get('fusion',{}).get('reason','invalid')}"]
                for j,text in enumerate(texts):
                    y=70+i*125+j*27
                    cv2.putText(canvas,text,(10,y),cv2.FONT_HERSHEY_SIMPLEX,.63,(0,0,0),4)
                    cv2.putText(canvas,text,(10,y),cv2.FONT_HERSHEY_SIMPLEX,.63,(0,255,255),1)
            if capture is not None and capture_label is not None:
                capture.write(json.dumps(dict(type='frame', label=capture_label,
                    expected=expected_by_label[capture_label], forbidden=['Open_Palm','Closed_Fist'] if capture_label=='relaxed_negative' else [], captured_at=packet.captured_at,
                    sequence=packet.sequence, hands=samples))+'\n')
                capture.flush()
                capture_frames += 1
            frames += 1
            now=time.monotonic()
            if now-last_report >= 1:
                hz=frames/(now-last_report)
                if args.terminal_capture:
                    if pending_label:
                        status = f'准备中，还剩{max(0,capture_start-now):.1f}秒'
                    elif capture_label:
                        status = f'采集中，还剩{max(0,capture_end-now):.1f}秒，已记录{capture_frames}帧'
                    else:
                        status = '暂停，输入1～9后回车开始'
                    print(f'{status} | {hz:.1f} Hz | 检测到{len(observations)}只手', flush=True)
                else:
                    print(json.dumps(dict(hz=round(hz,2), age_ms=round((now-packet.captured_at)*1000,1), hands=rows)),flush=True)
                frames=0
                last_report=now
            cv2.putText(canvas,f'READ ONLY | {hz:.1f} Hz | Original vs Aligned (experimental)',(10,30),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,255,255),2)
            if capture is not None:
                hint = f'1-3:Open 4-6:Fist 7:Relax 8:Victory 9:Thumb 0:Pause | {capture_label or "PAUSED"} {capture_frames}'
                cv2.rectangle(canvas,(0,675),(960,720),(0,0,0),-1)
                cv2.putText(canvas,hint,(8,700),cv2.FONT_HERSHEY_SIMPLEX,.52,(0,255,255),1)
            if not args.no_preview:
                cv2.imshow('Gesture comparison',canvas if args.windowed else fit_screen(canvas,1920,1080))
            delay=max(1,int((1/config.inference_hz-(time.monotonic()-cycle))*1000))
            if args.no_preview:
                time.sleep(delay / 1000)
                key = 255
            else:
                key = cv2.waitKey(delay)&255
            if capture is not None and not args.terminal_capture and chr(key) in ('0', *poses):
                capture_label = None if key == ord('0') else poses[chr(key)][0]
                capture_frames = 0
                print(f'CAPTURE: {capture_label or "PAUSED"}',flush=True)
            if key in (27,ord('q')):
                break
    finally:
        if capture is not None:
            capture.close()
        source.stop()
        hands.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
        print('STOPPED: camera released',flush=True)

if __name__ == '__main__':
    main()
