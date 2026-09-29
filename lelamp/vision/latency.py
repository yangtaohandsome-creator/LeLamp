"""Optional timestamp sink, disabled outside diagnostic sessions."""
recorder = None

def emit(kind, **fields):
    if recorder is not None:
        recorder.emit(dict(type=kind, **fields))
