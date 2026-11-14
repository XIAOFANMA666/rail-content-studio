"""Generic synthetic coverage, not production-reference hard-coded answers."""
import unittest

import numpy as np

import reference_cuts as cuts


class CutSelectionTests(unittest.TestCase):
    def detect(self, frames):
        metrics, count = cuts._metrics(frames, (96, 96))
        return cuts._select_cuts(metrics, 30, count)

    def test_uniform_hard_cut(self):
        red = np.zeros((96, 96, 3), np.uint8); red[:, :, 0] = 220
        blue = np.zeros_like(red); blue[:, :, 2] = 220
        self.assertEqual(self.detect([red]*20+[blue]*20), [0,20,40])

    def test_small_text_overlay_is_not_a_cut(self):
        base = np.full((96,96,3), 80, np.uint8)
        overlay = base.copy(); overlay[70:90,10:80] = 255
        self.assertEqual(self.detect([base]*20+[overlay]*20+[base]*20), [0,60])

    def test_one_frame_flash_is_not_a_shot(self):
        base = np.full((96,96,3), 40, np.uint8)
        flash = np.full_like(base,255)
        self.assertEqual(self.detect([base]*20+[flash]+[base]*20), [0,41])

    def test_continuous_motion_does_not_split(self):
        random = np.random.default_rng(21)
        base = random.integers(20,220,size=(96,96,3),dtype=np.uint8)
        frames = [np.roll(base,i*2,axis=1) for i in range(60)]
        self.assertEqual(self.detect(frames), [0,60])

    def test_fade_is_not_reported_as_hard_cuts(self):
        frames = [np.full((96,96,3),int(i*255/59),np.uint8) for i in range(60)]
        self.assertEqual(self.detect(frames), [0,60])

    def test_lower_contrast_broad_jump_cut_is_detected(self):
        random=np.random.default_rng(9)
        a=random.integers(40,120,size=(96,96,3),dtype=np.uint8)
        b=random.integers(40,120,size=(96,96,3),dtype=np.uint8)
        self.assertEqual(self.detect([a]*20+[b]*20),[0,20,40])

    def test_adjacent_secondary_layer_change_keeps_stronger_boundary(self):
        a=np.full((96,96,3),20,np.uint8)
        b=np.full_like(a,180)
        c=np.full_like(a,200)
        self.assertEqual(self.detect([a]*20+[b]+[c]*20),[0,20,41])

    def test_invalid_rate_and_frame_count_rejected_before_decode(self):
        for fps,total in [(0,30),(float('nan'),30),(30,False),(30,10801)]:
            with self.subTest(fps=fps,total=total), self.assertRaises(ValueError):
                cuts.detect_cuts('missing.mp4',{'width':96,'height':96,'fps':fps,'total_frames':total})


if __name__ == '__main__':
    unittest.main()
