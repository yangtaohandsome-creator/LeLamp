import unittest
from lelamp.vision.gesture_fusion_experiment import fuse_gestures

class FusionTests(unittest.TestCase):
    def test_union_deduplicates_and_retains_disagreement(self):
        n=('None',.99)
        self.assertEqual(fuse_gestures(n,('Open_Palm',.4),('Open_Palm',.6)).labels,('Open_Palm',))
        self.assertEqual(fuse_gestures(n,('Closed_Fist',.8),('Open_Palm',.7)).labels,('Closed_Fist','Open_Palm'))
    def test_thumb_up_only_original(self):
        n=('None',0)
        self.assertEqual(fuse_gestures(n,('Thumb_Up',.99),('Thumb_Up',.99)).labels,())
        self.assertEqual(fuse_gestures(('Thumb_Up',.6),n,('Open_Palm',.9)).labels,('Thumb_Up','Open_Palm'))
    def test_each_other_gesture_allowed_from_every_branch(self):
        for gesture in ('Open_Palm','Closed_Fist','Thumb_Down','Victory','Pointing_Up','ILoveYou'):
            for i in range(3):
                branches=[('None',0)]*3
                branches[i]=(gesture,.4)
                self.assertEqual(fuse_gestures(*branches).labels,(gesture,))
    def test_invalid_empty_and_no_history(self):
        n=('None',0)
        self.assertEqual(fuse_gestures(('Open_Palm',float('nan')),n,n).label,'None')
        self.assertEqual(fuse_gestures(('Open_Palm',.5),n,n).label,'Open_Palm')
        self.assertEqual(fuse_gestures(n,n,n).label,'None')
