import hashlib
from pathlib import Path
import tempfile
import unittest

from x1.core import search, audit_sensor, repair_sensor


SAMPLE = b'''*Heading\n*Step, name=First\n*Output, history, time interval=0.01\n*Contact Output, surface=FaceA\nCFNM,\n*End Step\n*Step, name=Second\n*Output, history, time interval=0.01\n*Contact Output, surface=FaceA\nCFNM,\n*End Step\n'''


class X1Test(unittest.TestCase):
    def test_search_known_and_unknown(self):
        self.assertTrue(any('网格' in x['title'] for x in search('网格质量有警告')['results']))
        self.assertEqual(search('qzx923unknown')['results'], [])

    def test_sensor_repair_keeps_original_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'a.inp'
            source.write_bytes(SAMPLE)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = repair_sensor(source, tmp, 'ContactSensor', 'FaceA', ['First', 'Second'])
            self.assertEqual(result['changed_lines'], [3, 8])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            checked = audit_sensor(result['output'])['history_outputs']
            self.assertEqual([b['sensor_name'] for b in checked], ['ContactSensor', 'ContactSensor'])
            again = repair_sensor(result['output'], tmp, 'ContactSensor', 'FaceA', ['First', 'Second'])
            self.assertEqual(again['changed_lines'], [])

    def test_wrong_surface_or_missing_step_cannot_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'a.inp'
            source.write_bytes(SAMPLE)
            with self.assertRaises(ValueError):
                repair_sensor(source, tmp, 'Name', 'WrongSurface', ['First'])
            with self.assertRaises(ValueError):
                repair_sensor(source, tmp, 'Name', 'FaceA', ['First', 'Third'])
            self.assertEqual(source.read_bytes(), SAMPLE)

    def test_reject_multiple_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'a.inp'
            source.write_bytes(SAMPLE.replace(b'CFNM,', b'CFNM, CFN1,'))
            with self.assertRaises(ValueError):
                repair_sensor(source, tmp, 'Name', 'FaceA', ['First'])


if __name__ == '__main__':
    unittest.main()
