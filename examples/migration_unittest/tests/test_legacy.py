import unittest


class ArithmeticTest(unittest.TestCase):
    def setUp(self):
        self.factor = 3

    def test_multiply(self):
        self.assertEqual(self.factor * 4, 12)

    @unittest.skip("documented legacy skip")
    def test_future_behavior(self):
        self.fail("skipped")
