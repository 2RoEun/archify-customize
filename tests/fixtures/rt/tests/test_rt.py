import unittest

from rtpkg import mod


class RT(unittest.TestCase):
    def test_all(self):
        self.assertEqual(mod.decorated(), 1)
        self.assertEqual(mod.use_box(), 1)
        self.assertEqual(mod.via_lambda(), 1)
        self.assertEqual(mod.via_genexpr(), 3)
        self.assertEqual(mod.dynamic(), 1)
        self.assertEqual(mod.threaded(), [1])
