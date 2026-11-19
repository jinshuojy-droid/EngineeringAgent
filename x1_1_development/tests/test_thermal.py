import tempfile
import unittest
from pathlib import Path
from x1.thermal import convert, audit, material_copy
from x1.core import search, digest


class ThermalTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(convert('specific_heat',385,'J/(kg*K)')['output_value'],3.85e8)
        self.assertEqual(convert('conductivity',385,'W/(m*K)')['output_value'],385)
        self.assertAlmostEqual(convert('density',8900,'kg/m3')['output_value'],8.9e-9)
        with self.assertRaises(ValueError):convert('specific_heat',385,'W/(m*K)')

    def test_knowledge(self):
        self.assertIn('热—力耦合',search('热力耦合怎么设置')['results'][0]['title'])
        self.assertIn('绝热',search('绝热加热怎么设置')['results'][0]['title'])

    def test_mechanical_and_adiabatic_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'job.inp';p.write_text('*Material, name=Copper\n*Density\n8.9e-9\n*Plastic\n100,0\n*Step, name=One\n*Dynamic, Explicit, adiabatic\n,0.1\n*End Step\n')
            r=audit(p);self.assertEqual(r['steps'][0]['type'],'adiabatic')
            self.assertIn('specific heat',r['findings'][0]['summary'])
            self.assertIn('inelastic heat fraction',r['findings'][0]['summary'])
            self.assertEqual(r['materials'][0]['properties']['density'][0]['line'],2)

    def test_coupled_and_missing_include(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'job.inp';p.write_text('*Element, type=C3D8RT\n1,1,2,3,4,5,6,7,8\n*Include, input=missing.inp\n*Step, name=One\n*Coupled Temperature-Displacement, Explicit\n,0.1\n*End Step\n')
            r=audit(p);self.assertFalse(r['complete']);self.assertTrue(r['recognized_coupled_element_types'])
            self.assertEqual(r['steps'][0]['type'],'coupled')

    def test_copy_is_restricted_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'src').mkdir();source=root/'src'/'job.inp'
            source.write_text('*Material, name=Copper\n*Density\n8.9e-9\n*Step, name=S1\n*End Step\n')
            before=digest(source);result=material_copy(source,root/'out','Copper',3.85e8,0.9)
            self.assertEqual(digest(source),before);self.assertEqual(result['source_sha256_after'],before)
            out=Path(result['output']).read_text();self.assertIn('*Specific Heat\n385000000,',out)
            self.assertIn('*Inelastic Heat Fraction\n0.9,',out)
            with self.assertRaises(ValueError):material_copy(source,root/'src','Copper',3.85e8,0.9)

    def test_existing_material_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'src').mkdir();source=root/'src'/'job.inp'
            source.write_text('*Material, name=Copper\n*Specific Heat\n1\n*Step, name=S1\n*End Step\n')
            with self.assertRaises(ValueError):material_copy(source,root/'out','Copper',1,0.9)

if __name__=='__main__':unittest.main()
