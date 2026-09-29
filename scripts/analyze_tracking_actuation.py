#!/usr/bin/env python3
"""Read existing motor A/B logs and simulate command quantization; no hardware IO.

Quantization experiments describe integer goal encoding only, NOT a motor or
closed-loop simulation. Low position error alone is not a smoothness metric.
"""
import argparse
import json
from pathlib import Path
import numpy as np


def rows(path):
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # Accept a copied live log with an incomplete final row.
    return result


def analyze_trial(samples):
    joint = samples[0]['joint']
    q = np.array([s['actual_raw'][joint] for s in samples], dtype=float)
    goal = np.array([s['goal_raw'][joint] for s in samples], dtype=float)
    actual_steps, goal_steps = np.diff(q), np.diff(goal)
    moving = actual_steps[actual_steps != 0]
    return dict(joint=joint, value=samples[0].get('p'), samples=len(samples),
        error_p50_p95=np.percentile(abs(goal-q), [50, 95]).tolist(),
        actual_step_p95_max=np.percentile(abs(actual_steps), [95, 100]).tolist(),
        zero_step_fraction=float(np.mean(actual_steps == 0)),
        actual_total_travel_ticks=float(sum(abs(actual_steps))),
        goal_total_travel_ticks=float(sum(abs(goal_steps))),
        reversals_excluding_zero=int(sum(np.diff(np.sign(moving)) != 0)))


def quantization_trials(ticks_per_unit):
    results = []
    for speed in (.1, .5, 1., 2., 8.):
        for hz in (25, 50, 100):
            t = np.arange(0., 10., 1 / hz)
            # Positive raw origin; int truncation matches the current bus mapping.
            raw = (2000 + speed * t * ticks_per_unit).astype(int)
            changes = np.diff(raw)
            results.append(dict(speed_units_per_second=speed, hz=hz,
                updates_per_second=float(np.count_nonzero(changes) / (t[-1]-t[0])),
                max_step_ticks=int(max(abs(changes))),
                seconds_to_accumulate_one_tick=1 / (speed * ticks_per_unit)))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('runtime_state/tracking_diagnostics'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = {'ab_trials': {}, 'quantization_only': {}, 'unit_conversion_only': {}}
    for path in sorted(args.directory.glob('servo-p-ab-*.jsonl')):
        data = rows(path)
        samples = [s for s in data if s['type'] == 'sample']
        metadata = {s['trial']: s for s in data if s['type'] == 'trial'}
        trials = []
        for trial in sorted(set(s['trial'] for s in samples)):
            active = [s for s in samples if s['trial'] == trial and s['phase_t'] <= 16]
            stat = analyze_trial(active)
            stat.update(trial=trial, register=metadata[trial].get('register', 'P_Coefficient'))
            trials.append(stat)
        results['ab_trials'][path.name] = trials
    baseline = rows(args.directory / 'servo-slow-1790579546777937612.jsonl')[0]
    for joint, maxv, maxa in [('base_yaw', 80, 210), ('wrist_pitch', 40, 140)]:
        cal = baseline['calibration'][joint]
        ticks = (cal['range_max'] - cal['range_min']) / 200
        results['quantization_only'][joint] = quantization_trials(ticks)
        results['unit_conversion_only'][joint] = dict(
            ticks_per_calibrated_unit=ticks,
            max_velocity_steps_per_second=maxv*ticks,
            acceleration_steps_per_second_squared=maxa*ticks,
            acceleration_register_equivalent=maxa*ticks/100,
            current_acceleration_register=baseline['values'][joint]['Acceleration'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(args.output)


if __name__ == '__main__':
    main()
