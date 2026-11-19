"""X1.3 对照、截面映射与副本编辑回归。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from x1.edit import material_write, interaction_edit, semantic_diff, excel_material_spec
from x1.thermal import audit


DECK = '''*Part, name=Body
*Element, type=C3D8RT
1, 1,2,3,4,5,6,7,8
2, 9,10,11,12,13,14,15,16
*Elset, elset=Group, generate
1, 2, 1
*Solid Section, elset=Group, material=Mat, controls=Control
,
*End Part
*Section Controls, name=Control, hourglass=ENHANCED
*Material, name=Mat
*Density
5.,
*Specific Heat
10.,
** Keep this comment
*Plastic, hardening=JOHNSON COOK
1,2,3,4,5,6
*Rate Dependent, type=JOHNSON COOK
1,2
*Surface Interaction, name=Contact
*Friction
0.3,
*Gap Heat Generation
0.8, 0.5, , , , , ,
*Surface Interaction, name=Other
*Friction
0.4,
*Step, name=Step-1
*Dynamic Temperature-displacement, Explicit
,1
*End Step
'''


class X13Tests(unittest.TestCase):
    def setup_files(self, root, spec):
        root = Path(root); source = root/'src'/'job.inp'; source.parent.mkdir()
        source.write_text(DECK, encoding='ascii')
        path = root/'spec.json'; path.write_text(json.dumps(spec),encoding='utf-8')
        return source, path, root/'out'

    def mat(self, properties):
        return {'material':'Mat', 'unit_system':'user-confirmed', 'source':'test fixture', 'properties':properties}

    def contact(self, changes):
        return {'interaction':'Contact', 'unit_system':'user-confirmed', 'source':'test fixture', 'changes':changes}

    def test_full_material_update_scoped_and_traced(self):
        props={'density':{'rows':[[6]]}, 'elastic':{'rows':[[100, .3, 20],[90,.3,100]]},
               'conductivity':{'rows':[[12,20],[14,100]]}, 'specific_heat':{'rows':[[15,20],[17,100]]},
               'expansion':{'zero':20, 'rows':[[1e-5,20],[2e-5,100]]},
               'plastic_johnson_cook':{'rows':[[1,2,3,4,8,6]]},
               'rate_johnson_cook':{'rows':[[1,2]]}, 'inelastic_heat_fraction':{'rows':[[.8]]}}
        with tempfile.TemporaryDirectory() as tmp:
            source,spec,out=self.setup_files(tmp,self.mat(props)); result=material_write(source,spec,out)
            self.assertEqual(source.read_text(),DECK)
            edited=Path(result['output']).read_text()
            self.assertIn('*Conductivity\n12, 20\n14, 100',edited)
            self.assertIn('** Keep this comment',edited)
            self.assertIn('*Surface Interaction, name=Other\n*Friction\n0.4',edited)
            self.assertEqual(result['source_sha256'],semantic_diff(source,result['output'])['before_sha256'])
            self.assertTrue((Path(result['output']).parent/'修改对照.diff').exists())

    def test_interaction_single_variable_keeps_other_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,spec,out=self.setup_files(tmp,self.contact({'heat_conversion':1}))
            result=interaction_edit(source,spec,out); edited=Path(result['output']).read_text()
            self.assertIn('*Gap Heat Generation\n1, 0.5, , , , , ,',edited)
            self.assertIn('*Friction\n0.3,',edited)
            changes=result['semantic_diff']['card_changes']
            self.assertEqual(len(changes),1)
            self.assertEqual(changes[0]['before'][0]['keyword'],'gap heat generation')
            self.assertEqual(changes[0]['data_changes'][0]['field'],'heat_conversion')
            self.assertTrue(result['outside_target_identical'])
            self.assertEqual(source.read_text(),DECK)

    def test_friction_and_pressure_conductance(self):
        changes={'friction':.6,'gap_conductance':{'dependency':'pressure','rows':[[2,0],[3,10]]}}
        with tempfile.TemporaryDirectory() as tmp:
            source,spec,out=self.setup_files(tmp,self.contact(changes)); result=interaction_edit(source,spec,out)
            text=Path(result['output']).read_text()
            self.assertIn('*Gap Conductance, pressure\n2, 0\n3, 10',text)
            self.assertIn('*Friction\n0.6,',text)
            changed=[x['keyword'] for group in result['semantic_diff']['card_changes'] for x in group['after']]
            self.assertIn('friction',changed); self.assertIn('gap conductance',changed)

    def test_validate_inputs_and_do_not_write_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,spec,out=self.setup_files(tmp,self.mat({'specific_heat':{'rows':[[2,10],[3,5]]}}))
            with self.assertRaises(ValueError): material_write(source,spec,out)
            self.assertFalse(out.exists()); self.assertEqual(source.read_text(),DECK)
            spec.write_text(json.dumps(self.contact({'heat_conversion':1.1})))
            with self.assertRaises(ValueError): interaction_edit(source,spec,out)
            spec.write_text(json.dumps(self.contact({'friction':.4})))
            with self.assertRaises(ValueError): interaction_edit(source,spec,source.parent)
            self.assertEqual(source.read_text(),DECK)

    def test_reject_include_and_complex_existing_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,spec,out=self.setup_files(tmp,self.contact({'friction':.2}))
            source.write_text(DECK+'*Include, input=missing.inp\n')
            with self.assertRaises(ValueError): interaction_edit(source,spec,out)
            self.assertFalse(semantic_diff(source,source)['complete'])
            source.write_text(DECK.replace('*Friction\n0.3,','*Friction, temperature dependency=YES\n0.3, 20'))
            with self.assertRaises(ValueError): interaction_edit(source,spec,out)

    def test_section_controls_and_unknown_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,_,_=self.setup_files(tmp,self.mat({'density':{'rows':[[1]]}}))
            s=audit(source)['sections'][0]
            self.assertEqual(s['part'],'Body'); self.assertEqual(s['hourglass'],'ENHANCED')
            self.assertEqual(s['element_types'],[{'type':'C3D8RT','count':2}]); self.assertTrue(s['complete'])
            source.write_text(DECK.replace('*Solid Section, elset=Group, material=Mat, controls=Control\n,',
                                           '*Solid Section, elset=Group, material=Mat, controls=Control\n,\n*Hourglass Stiffness\n2.'))
            self.assertIn('hourglass_stiffness',audit(source)['sections'][0])
            source.write_text(DECK.replace('elset=Group, material=Mat','elset=Missing, material=Mat'))
            self.assertFalse(audit(source)['sections'][0]['complete'])

    def test_format_only_diff_and_comment_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,_,_=self.setup_files(tmp,self.mat({'density':{'rows':[[1]]}}))
            other=Path(tmp)/'other.inp'; other.write_text(DECK.replace('0.3,','0.3000,').replace('Keep this','Alter this'))
            data=semantic_diff(source,other)
            self.assertTrue(data['card_semantics_equal']); self.assertFalse(data['raw_bytes_equal'])

    def test_cli_retains_unified_entry(self):
        root=Path(__file__).resolve().parents[1]
        proc=subprocess.run([sys.executable,str(root/'x1.py'),'--version'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0); self.assertIn('X1.6',proc.stdout)

    def test_excel_explicit_mapping_and_unchanged_workbook(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); raw=root/'src'; raw.mkdir(); xlsx=raw/'source.xlsx'
            book=openpyxl.Workbook(); sheet=book.active; sheet.title='Data'
            sheet.append([10,20]); sheet.append([15,50]); book.save(xlsx); book.close()
            initial=xlsx.read_bytes()
            mapping=self.mat({'specific_heat':{'sheet':'Data','range':'A1:B2'}})
            path=root/'mapping.json'; path.write_text(json.dumps(mapping),encoding='utf-8')
            result=excel_material_spec(xlsx,path,root/'out')
            spec=json.loads(Path(result['output']).read_text(encoding='utf-8'))
            self.assertEqual(spec['properties']['specific_heat']['rows'],[[10,20],[15,50]])
            self.assertEqual(initial,xlsx.read_bytes())
            mapping['properties']['specific_heat']['range']='A1:C2'; path.write_text(json.dumps(mapping))
            with self.assertRaises(ValueError): excel_material_spec(xlsx,path,root/'out')


if __name__ == '__main__': unittest.main()
