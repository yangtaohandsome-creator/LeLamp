"""Restore the saved homing pose; keep torque enabled for visual inspection."""
import argparse
import time

from lelamp.follower import LeLampFollower, LeLampFollowerConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', default='lamppi')
    parser.add_argument('--port', default='/dev/ttyACM0')
    parser.add_argument('--move', action='store_true', help='Actually move; otherwise only read positions')
    parser.add_argument('--range-center', action='store_true', help='Use range midpoint instead of saved homing pose')
    args = parser.parse_args()
    robot = LeLampFollower(LeLampFollowerConfig(id=args.id, port=args.port))
    bus = robot.bus
    moving = False
    bus.connect()
    try:
        if not robot.calibration or bus.read_calibration() != robot.calibration:
            raise RuntimeError('Hardware calibration differs from file. No movement performed.')
        start = bus.sync_read('Present_Position', normalize=False)
        target = {}
        for name, cal in robot.calibration.items():
            target[name] = (cal.range_min + cal.range_max) // 2 if args.range_center else 2047
            if not cal.range_min <= start[name] <= cal.range_max:
                raise RuntimeError(f'{name}: current position outside saved range; no movement')
            if not cal.range_min <= target[name] <= cal.range_max:
                raise RuntimeError(f'{name}: target outside saved range; no movement')
            if bus.read('Operating_Mode', name, normalize=False) != 0:
                raise RuntimeError(f'{name}: not in position mode; no movement')
            print(f'{name}: {start[name]} -> {target[name]}', flush=True)
        if not args.move:
            print('Read only. Add --move to move slowly and hold.')
            return
        # Prime the target before enabling torque, avoiding an old target jump.
        bus.sync_write('Goal_Position', start, normalize=False)
        moving = True
        bus.enable_torque()
        steps = max(40, max(abs(target[n] - start[n]) for n in start))
        # At most one encoder tick per 25 ms: about 3.5 degrees/second.
        for step in range(1, steps + 1):
            goal = {n: round(start[n] + (target[n] - start[n]) * step / steps) for n in start}
            actual = bus.sync_read('Present_Position', normalize=False)
            if any(abs(actual[n] - goal[n]) > 120 for n in goal):
                raise RuntimeError('Tracking error exceeded 120 ticks; movement stopped.')
            bus.sync_write('Goal_Position', goal, normalize=False)
            time.sleep(0.006)
        time.sleep(1)
        actual = bus.sync_read('Present_Position', normalize=False)
        print('Final positions:', actual, flush=True)
        if any(abs(actual[n] - target[n]) > 30 for n in target):
            raise RuntimeError('Target not reached within 30 ticks.')
        print('Pose reached. Torque remains ON for inspection. Support lamp before disabling power.', flush=True)
    except BaseException:
        if moving:
            try:
                actual = bus.sync_read('Present_Position', normalize=False)
                bus.sync_write('Goal_Position', actual, normalize=False)
                print('Stopped at current pose; torque remains ON.', flush=True)
            except Exception:
                print('Unable to send stop target. Support lamp and cut motor power.', flush=True)
        raise
    finally:
        bus.disconnect(disable_torque=False)


if __name__ == '__main__':
    main()
