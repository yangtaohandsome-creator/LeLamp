#!/usr/bin/env python3
"""SSH guided tracking diagnostic with the existing HDMI follow preview."""
import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lelamp.motion import tracking_diagnostics
from lelamp.vision import latency
from vision_preview import run

async def main():
    directory = Path('runtime_state/tracking_diagnostics')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'tracking-{time.time_ns()}.jsonl'
    recorder = tracking_diagnostics.Recorder(path)
    tracking_diagnostics.active_recorder = latency.recorder = recorder
    loop = asyncio.get_running_loop()
    commands = asyncio.Queue()
    def ready():
        line = sys.stdin.readline()
        commands.put_nowait(line.strip().lower() if line else 'q')
    loop.add_reader(sys.stdin, ready)
    args = argparse.Namespace(follow=True, snapshot=None, windowed=False,
        hand_calibration=False, screen_width=1920, screen_height=1080)
    preview = asyncio.create_task(run(args))
    phase = None
    deadline = 0
    cues = []
    menu = ('\n1=握拳保持不动  2=握拳匀速左右移动  3=快速反向（有提示）'
            '\n4=快速移手后停住（有提示）  5=静止后快速起步（有提示）'
            '\n6=中心附近慢速左右小幅移动  7=中心附近慢速上下小幅移动（有提示）'
            '\nj=标记抖动/顿挫  l=标记跟不上/启动慢  0=结束分段  q=安全退出（均需回车）')
    print(f'记录文件：{path}\n等待跟随启动，先用近距离张开→握拳进入手部跟随。'
          '确认预览source=hand后再开始记录分段。测试中保持握拳，避免张开触发锁定。'
          '普通分段15秒，6/7慢速分段25秒；结束分段仍在跟随，q才停止。'+menu, flush=True)
    try:
        while not preview.done():
            try:
                command = await asyncio.wait_for(commands.get(), .2)
            except asyncio.TimeoutError:
                command = None
            now = time.monotonic()
            while cues and now >= cues[0][0]:
                _, cue, message = cues.pop(0)
                recorder.emit(dict(type='motion_cue', phase=phase, cue=cue))
                print(message, flush=True)
            if phase and now >= deadline:
                recorder.emit(dict(type='phase_end', phase=phase))
                print('本段已结束。'+menu, flush=True)
                phase = None
            if command == 'q':
                preview.cancel()
                break
            if command in ('1','2','3','4','5','6','7','0'):
                if phase:
                    recorder.emit(dict(type='phase_end', phase=phase))
                phase = {'1':'stationary','2':'constant_motion','3':'fast_reversal','4':'hand_fast_stop','5':'hand_fast_start','6':'hand_slow_small_horizontal','7':'hand_slow_small_vertical'}.get(command)
                cues = []
                if phase:
                    duration = 25 if command in ('6', '7') else 15
                    deadline = now+duration
                    recorder.emit(dict(type='phase_start', phase=phase, duration=duration))
                    print(f'本段{duration}秒。保持握拳、手留在画面内。', flush=True)
                    if command in ('6', '7'):
                        axis = '左右' if command == '6' else '上下'
                        print('准备：把拳头放在画面中心附近，保持距离不变，静止5秒。', flush=True)
                        cues = [(now+5, 'slow_small_start', f'现在：保持握拳，缓慢{axis}移动几厘米再返回；单程约3～4秒，反复小幅移动，不要追着灯头调整。'),
                                (now+20, 'stationary_end', '现在：停住保持握拳5秒，观察灯头是否仍然一跳一跳。')]
                    elif command in ('3', '4', '5'):
                        print('准备：保持握拳不动3秒，等“现在”提示。', flush=True)
                        if command == '3':
                            cues = [(now+3, 'start', '现在：握拳向一侧快速移动。'),
                                    (now+4, 'reverse', '现在：立即反向快速移动，然后停住保持握拳。')]
                        elif command == '4':
                            cues = [(now+3, 'move_stop', '现在：握拳快速移到另一位置，随即停住，保持到本段结束。')]
                        else:
                            cues = [(now+3, 'start', '现在：握拳快速向一侧移动，到画面边缘前停住；保持到本段结束。')]
                    else:
                        print('现在：'+('保持握拳不动。' if command == '1' else '握拳匀速左右移动。'), flush=True)
                else:
                    print('分段已结束，运动仍在跟随。'+menu, flush=True)
            elif command in ('j', 'l'):
                recorder.emit(dict(type='jitter_mark' if command == 'j' else 'lag_mark', phase=phase))
                print('已标记；标记仅供定位，分析会查看前后数据。', flush=True)
            elif command is not None:
                print(menu, flush=True)
        try:
            await preview
        except asyncio.CancelledError:
            pass
    finally:
        loop.remove_reader(sys.stdin)
        if not preview.done():
            preview.cancel()
            try:
                await preview
            except asyncio.CancelledError:
                pass
        tracking_diagnostics.active_recorder = latency.recorder = None
        await asyncio.to_thread(recorder.close)
        print(f'记录已结束：{path}，丢记录={recorder.dropped}，写入错误={recorder.error}', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
