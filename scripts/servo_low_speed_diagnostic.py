#!/usr/bin/env python3
"""Bounded motor-only test; no PID/config/calibration writes or vision models."""
import asyncio
import fcntl
import math
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lelamp.motion.controller import MotionController
from lelamp.motion.config import motion_port, base_yaw_left_sign
from lelamp.motion.heading import load_heading
from lelamp.motion.visual_tracking import load_visual_tracking_config, CONTROLLED_JOINTS
from lelamp.motion.tracking_diagnostics import Recorder
from lelamp.follower import LeLampFollower, LeLampFollowerConfig

REGISTERS = ('Operating_Mode','P_Coefficient','I_Coefficient','D_Coefficient',
    'CW_Dead_Zone','CCW_Dead_Zone','Minimum_Startup_Force','Acceleration',
    'Goal_Velocity','Goal_Time','Present_Voltage','Present_Temperature')

async def run():
    port=motion_port()
    lock=open('/tmp/lelamp-'+Path(port).name+'.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    robot=LeLampFollower(LeLampFollowerConfig(port=port,id='lamppi'))
    bus=robot.bus
    motion=MotionController(port=port,robot=robot)
    stop=asyncio.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig,stop.set)
    directory=Path('runtime_state/tracking_diagnostics');directory.mkdir(parents=True,exist_ok=True)
    path=directory/f'servo-slow-{time.time_ns()}.jsonl'
    recorder=Recorder(path)
    moved=False
    print(f'Recording: {path}',flush=True)
    try:
        # Direct bus connection skips follower.configure(), which normally rewrites PID.
        bus.connect(handshake=False)
        config={j:{k:bus.read(k,j,normalize=False) for k in REGISTERS} for j in CONTROLLED_JOINTS}
        recorder.emit(dict(type='registers',values=config,calibration={j:asdict(bus.calibration[j]) for j in CONTROLLED_JOINTS}))
        if any(v['Operating_Mode']!=0 for v in config.values()):
            raise RuntimeError('Expected position mode; refusing to change mode')
        motion.set_base_yaw_offset_degrees(load_heading()*base_yaw_left_sign())
        cfg=load_visual_tracking_config()
        lower,upper=motion.tracking_bounds(cfg.minimum,cfg.maximum)
        if stop.is_set():return
        moved=True
        await motion.tracking_home()
        await asyncio.sleep(1)
        center=motion.read_action()
        amplitude=4.0
        for i,j in enumerate(CONTROLLED_JOINTS):
            if not lower[i]<=center[j+'.pos']-amplitude<=center[j+'.pos']+amplitude<=upper[i]:
                raise RuntimeError('Test envelope outside tracking limits')
        for joint in CONTROLLED_JOINTS:
            if stop.is_set():break
            print(f'{joint}: +/-4 calibration units, 16s smooth cycle, then 2s hold',flush=True)
            start=time.monotonic()
            for index in range(451):
                if stop.is_set():break
                action=dict(center)
                # Starts/ends at center with zero velocity. Peak speed ~1.81 units/s.
                t=min(index/25,16)
                offset=amplitude*math.sin(2*math.pi*t/16)**3
                action[joint+'.pos']+=offset
                await asyncio.sleep(max(0,start+index/25-time.monotonic()))
                if stop.is_set():break
                written=time.monotonic()
                motion.send_tracking_action(action)
                goal=bus.sync_read('Goal_Position',list(CONTROLLED_JOINTS),normalize=False)
                actual=bus.sync_read('Present_Position',list(CONTROLLED_JOINTS),normalize=False)
                recorder.emit(dict(type='sample',joint=joint,phase_t=index/25,
                    write_at=written,read_at=time.monotonic(),command=action,goal_raw=goal,actual_raw=actual))
        recorder.emit(dict(type='registers_after',values={j:{k:bus.read(k,j,normalize=False) for k in REGISTERS} for j in CONTROLLED_JOINTS}))
    finally:
        try:
            if moved and bus.is_connected:await motion.sleep()
        finally:
            motion.close()
            lock.close()
            await asyncio.to_thread(recorder.close)
            print(f'Finished: {path}; dropped={recorder.dropped}; error={recorder.error}',flush=True)

if __name__=='__main__':asyncio.run(run())
