"""Compare extracted history with the pre-extraction algorithm's event streams."""
import itertools
import unittest
from lelamp.vision.exit_sequence import ExitGestureSequence


class ExitSequenceTests(unittest.TestCase):
    def test_original_history_and_completion_equivalence(self):
        options = list(itertools.product(('Open_Palm','Closed_Fist'), ('a','b'), (False,True)))
        for events in itertools.product(options, repeat=4):
            for gap in (.2, 1., 3.1):
                old=[];new=ExitGestureSequence()
                for i,(gesture,hand,baseline) in enumerate(events):
                    stamp=i*gap
                    if old and old[-1][1]!=hand:old.clear()
                    event=(gesture,hand,stamp,baseline)
                    if old and old[-1][0]==gesture:old[-1]=event
                    else:old.append(event)
                    while old and stamp-old[0][2]>3:old.pop(0)
                    old=old[-4:]
                    labels=tuple(x[0] for x in old)
                    expected=labels[-4:]==('Closed_Fist','Open_Palm','Closed_Fist','Open_Palm') or (
                        labels[-3:]==('Open_Palm','Closed_Fist','Open_Palm') and old[-3][3])
                    new.add(gesture,hand,stamp,3,hold_baseline=baseline)
                    self.assertEqual([(x.gesture,x.hand_track_id,x.confirmed_at,x.hold_baseline) for x in new],old)
                    self.assertEqual(new.complete(),expected)
