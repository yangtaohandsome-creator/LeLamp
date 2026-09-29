"""Pure same-hand gesture history shared by otherwise independent sessions."""
from dataclasses import dataclass


@dataclass
class ExitGesture:
    gesture: str
    hand_track_id: str
    confirmed_at: float
    hold_baseline: bool = False


class ExitGestureSequence(list):
    def add(self, gesture, hand_track_id, confirmed_at, window, *, hold_baseline=False):
        if self and self[-1].hand_track_id != hand_track_id:
            self.clear()
        event = ExitGesture(gesture, hand_track_id, confirmed_at, hold_baseline)
        if self and self[-1].gesture == gesture:
            self[-1] = event
        else:
            self.append(event)
        while self and confirmed_at - self[0].confirmed_at > window:
            self.pop(0)
        if len(self) > 4:
            del self[:-4]

    def complete(self):
        gestures = tuple(item.gesture for item in self)
        return gestures[-4:] == ('Closed_Fist', 'Open_Palm', 'Closed_Fist', 'Open_Palm') or (
            gestures[-3:] == ('Open_Palm', 'Closed_Fist', 'Open_Palm') and self[-3].hold_baseline
        )
