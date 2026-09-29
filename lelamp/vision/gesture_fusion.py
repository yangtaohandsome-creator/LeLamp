"""Per-frame label union; does not emit app or motion events."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class FusionResult:
    label: str = 'None'
    labels: tuple[str, ...] = ()
    source: str = ''
    reason: str = 'no_candidate'


def fuse_gestures(original, aligned, roll_z):
    """Union of branch winners; only Original may contribute Thumb_Up.

    No extra confidence gate, time confirmation, or conflict arbitration.
    Multiple labels remain multiple candidates, not a new gesture class.
    """
    labels, sources = [], []
    supported = {'Open_Palm', 'Closed_Fist', 'Thumb_Up', 'Thumb_Down',
                 'Victory', 'Pointing_Up', 'ILoveYou'}
    for source, (label, score) in [('original', original), ('aligned', aligned), ('roll_z', roll_z)]:
        if label not in supported or not math.isfinite(score) or not 0 <= score <= 1:
            continue
        if label == 'Thumb_Up' and source != 'original':
            continue
        if label not in labels:
            labels.append(label)
        sources.append(source)
    if not labels:
        return FusionResult()
    return FusionResult(' | '.join(labels), tuple(labels), ','.join(sources), 'union')
