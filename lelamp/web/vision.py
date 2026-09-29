"""Read-only projection of existing app state for the LAN console."""
import math


def vision_view(app, maintenance_active=False):
    state = app.get_vision_state()
    snapshot = state.get('snapshot') or {}
    session = state.get('tracking_session') or {}
    runner = state.get('motion_tracking') or {}
    task = app.current_motion_task
    requested = app.current_mode == 'tracking' or (app.current_mode == 'work_light' and bool(session))
    active = requested and app.tracking and task is not None and not task.done()
    motion_error = None
    if requested and task is not None and task.done() and not task.cancelled():
        error = task.exception()
        if error is not None:
            motion_error = str(error) or type(error).__name__
    age = snapshot.get('result_age_ms')
    fresh = bool(state.get('running') and isinstance(age, (int, float))
                 and math.isfinite(age) and 0 <= age <= state.get('result_max_age_ms', 250))
    hands = snapshot.get('hands') or []
    return {
        'simulation': bool(getattr(app, 'simulation', False)),
        'enabled': bool(state.get('enabled', True)),
        'running': bool(state.get('running')),
        'status': state.get('status', 'stopped'),
        'error': motion_error or state.get('error') or (state.get('camera') or {}).get('error'),
        'camera_status': (state.get('camera') or {}).get('status', 'stopped'),
        'inference_hz': state.get('inference_hz', 0) if state.get('running') else 0,
        'result_age_ms': age,
        'fresh': fresh,
        'face_detection_enabled': state.get('face_detection_enabled', True),
        'face_count': snapshot.get('face_count', 0) if fresh else None,
        'hand_count': snapshot.get('hand_count', 0) if fresh else None,
        'gestures': [dict(track_id=h.get('track_id'), gesture=h.get('gesture'),
                          confidence=h.get('gesture_confidence')) for h in hands] if fresh else [],
        'work_light_hand_enabled': bool(state.get('work_light_hand_enabled')),
        'lost_remaining_seconds': session.get('lost_remaining_seconds'),
        'target_ambiguous': bool(session.get('target_ambiguous')),
        'hand_control_calibrated': bool(state.get('hand_control_calibrated')),
        'current_mode': app.current_mode,
        'maintenance_active': bool(maintenance_active),
        'tracking_requested': requested,
        'tracking_active': bool(active),
        'tracking_status': runner.get('status') if requested else None,
        'tracking_source': session.get('tracking_source') if requested else None,
        'tracking_phase': session.get('tracking_phase') if requested else None,
        'directive': runner.get('directive') if active else None,
        'target_visible': bool(active and fresh and runner.get('target_visible')),
    }
