"""X1.2 真实问题的回归：类型、区域、来源及安全边界。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from x1.core import search
from x1.journal import add, show
from x1.review import make_review, export_review, render
from x1.thermal import audit


EXPLICIT = '''*Part, name=P1
*Node
1, 0,0,0
2, 1,0,0
*Element, type=C3D8RT
1, 1,2
*Solid Section, elset=E, material=Cu
*End Part
*Assembly, name=Assembly
*Instance, name=P1-1, part=P1
*End Instance
*Nset, nset=SET-T, instance=P1-1, generate
1, 2, 1
*End Assembly
*Material, name=Cu
*Density
8.9e-9
*Conductivity
385
*Specific Heat
3.85e8
*Plastic, hardening=JOHNSON COOK
90,292,.31,1.09,1082.85,20.85
*Surface Interaction, name=Prop
*Friction
.5
*Initial Conditions, type=TEMPERATURE
SET-T, 20
*Step, name=Step-1
*Dynamic Temperature-displacement, Explicit
,0.01
*Contact Pair, interaction=Prop
S1,S2
*Output, field
*Node Output
NT
*End Step
*Step, name=Step-2
*Dynamic Temperature-displacement, Explicit
,0.1
*Output, field
*Node Output
NT
*End Step
'''


class X12Tests(unittest.TestCase):
    def setup_file(self, root, text=EXPLICIT):
        path=Path(root)/'source'/'job.inp';path.parent.mkdir();path.write_text(text,encoding='utf-8')
        return path

    def test_actual_explicit_keyword_and_unknown_never_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp,EXPLICIT+'*Step, name=Other\n*Unknown Procedure\n*End Step\n')
            result=audit(path)
            self.assertEqual([s['type'] for s in result['steps']],['coupled','coupled','unknown'])
            self.assertFalse(result['complete'])
            self.assertTrue(result['read_complete'])
            self.assertTrue(any('未识别' in f['summary'] for f in result['findings']))

    def test_unrecognized_earlier_step_preserves_original_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            text='*Step, name=UnknownFirst\n*Mystery Procedure\n*End Step\n'+EXPLICIT
            result=audit(self.setup_file(tmp,text))
            self.assertEqual([x['name'] for x in result['steps']],['UnknownFirst','Step-1','Step-2'])
            self.assertFalse(result['analysis_recognition_complete'])

    def test_mode_gates_adiabatic_advice(self):
        results=search('非弹性热份额 塑性生热',mode='coupled')['results']
        self.assertTrue(any(x['title']=='耦合分析的非弹性热份额' for x in results))
        self.assertFalse(any(x['title']=='绝热分析和塑性生热' for x in results))
        self.assertIn('绝热',search('绝热加热怎么设置',mode='adiabatic')['results'][0]['title'])

    def test_review_resolves_nodes_material_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp)
            data=make_review(path)
            self.assertEqual(data['regions']['instances'][0]['sections'][0]['material'],'Cu')
            self.assertEqual(data['regions']['initial_temperature'][0]['coverage'][0]['missing'],0)
            self.assertEqual([s['status'] for s in data['outputs']['temperature_by_step']],['已显式请求','已显式请求'])
            self.assertEqual(data['unit_system'],'未确认')
            self.assertEqual(data['files_changed'],[])
            self.assertIn('未显式设置',data['contact']['properties'][0]['heat_status'])
            self.assertIn('默认',data['contact']['properties'][0]['heat_status'])

    def test_partial_temperature_is_unknown_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp,EXPLICIT.replace('1, 2, 1\n*End Assembly','1, 1, 1\n*End Assembly'))
            data=make_review(path)
            self.assertEqual(data['regions']['initial_temperature'][0]['coverage'][0]['missing'],1)
            self.assertTrue(any('未证明覆盖' in f['summary'] for f in data['findings']))

    def test_missing_heat_material_and_jc_zero_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            text=EXPLICIT.replace('*Conductivity\n385\n','').replace('90,292,.31,1.09,1082.85,20.85','90,292,.31,0,0,0')
            data=make_review(self.setup_file(tmp,text),unit_system='N-mm-s-tonne-C')
            summaries='\n'.join(x['summary'] for x in data['findings'])
            self.assertIn('conductivity',summaries)
            self.assertIn('m 为零',summaries)
            self.assertIn('绝对零度',summaries)

    def test_absent_output_not_claimed_to_be_missing_in_solver(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp,EXPLICIT.replace('*Node Output\nNT\n',''))
            data=make_review(path)
            self.assertIn('未找到显式',data['outputs']['temperature_by_step'][0]['status'])
            self.assertTrue(any('默认值和跨步传播' in f['action'] for f in data['findings']))

    def test_provenance_flags_numeric_mismatch_and_retains_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp);record=Path(tmp)/'source.json'
            record.write_text(json.dumps({'materials':{'Cu':{'conductivity':{'value':400,'unit':'x','source':'test','status':'试算'},
                'plastic':{'value':90,'parameters':{'m':1.0}}}}}),encoding='utf-8')
            data=make_review(path,record)
            warning='\n'.join(x['message'] for x in data['materials']['warnings'])
            self.assertIn('数值与输入不一致',warning)
            self.assertIn('m 与输入不一致',warning)
            self.assertIn('试算',render(data))

    def test_include_missing_is_never_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=make_review(self.setup_file(tmp,EXPLICIT+'*Include, input=missing.inp\n'))
            self.assertTrue(data['input_read_warnings'])

    def test_include_sensor_coverage_is_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp,EXPLICIT+'*Include, input=included.inp\n')
            (path.parent/'included.inp').write_text('** separate file\n',encoding='utf-8')
            data=make_review(path)
            self.assertTrue(any(f['status']=='unknown' and '传感器登记检查只扫描主输入文件' in f['summary']
                                for f in data['findings']))

    def test_disjoint_initial_temperature_sets_are_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            text=EXPLICIT.replace('1, 2, 1\n*End Assembly',
                 '1, 1, 1\n*Nset, nset=SET-U, instance=P1-1\n2,\n*End Assembly')
            text=text.replace('SET-T, 20\n*Step','SET-T, 20\nSET-U, 20\n*Step')
            data=make_review(self.setup_file(tmp,text))
            self.assertEqual(data['regions']['coverage_summary'][0]['missing'],0)
            self.assertFalse(any('未证明覆盖' in x['summary'] for x in data['findings']))

    def test_overlapping_different_temperatures_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            text=EXPLICIT.replace('SET-T, 20\n*Step','SET-T, 20\nSET-T, 30\n*Step')
            data=make_review(self.setup_file(tmp,text))
            self.assertEqual(data['regions']['coverage_summary'][0]['conflicts'],2)
            self.assertTrue(any('不同温度初值' in f['summary'] for f in data['findings']))

    def test_logs_are_not_proof_of_matching_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp);path.with_suffix('.com').write_text("'datacheck': 1,\n",encoding='ascii')
            data=make_review(path)
            self.assertTrue(data['logs']['files'][0]['datacheck'])
            self.assertIn('不足以证明',data['logs']['association'])

    def test_application_name_identifies_analysis_without_datacheck_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp);path.with_suffix('.com').write_text("'applicationName': 'analysis',\n",encoding='ascii')
            result=make_review(path)['logs']['files'][0]
            self.assertFalse(result['datacheck'])
            self.assertEqual(result['datacheck_basis'],'applicationName 记录')

    def test_incompatible_absolute_zero_is_flagged_only_for_confirmed_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp,EXPLICIT.replace('*Initial Conditions','*Physical Constants, absolute zero=0\n*Initial Conditions'))
            data=make_review(path,unit_system='N-mm-s-tonne-C')
            self.assertTrue(any('绝对零度与用户选择' in x['summary'] for x in data['findings']))
            data=make_review(path)
            self.assertFalse(any('绝对零度与用户选择' in x['summary'] for x in data['findings']))

    def test_subroutine_sensor_and_amplitude_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub=Path(tmp)/'u.f';sub.write_text("      IF (AMPNAME .EQ. 'MYAMP') THEN\n      X=VGETSENSORVALUE('SENSE',T,V)\n",encoding='ascii')
            text=EXPLICIT.replace('*Step, name=Step-1', '*Amplitude, name=MyAmp, definition=USER\n*Step, name=Step-1')
            text=text.replace('*Output, field\n*Node Output\nNT\n',
                 '*Output, history, sensor, name=SENSE\n*Node Output, nset=RP\nU3\n*Output, field\n*Node Output\nNT\n')
            path=self.setup_file(tmp,text);path.with_suffix('.com').write_text("'user': %r,\n'datacheck': 1,\n"%str(sub),encoding='ascii')
            data=make_review(path)
            self.assertEqual([x['matches'] for x in data['subroutine']['sensors'][0]['steps']],[1,1])
            self.assertTrue(data['subroutine']['amplitude_comparisons'][0]['match_in_source'])

    def test_unified_cli_review_and_contextual_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp);root=Path(__file__).resolve().parents[1];entry=root/'x1.py'
            result=subprocess.run([sys.executable,str(entry),'ask','塑性生热','--inp',str(path)],
                                  capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1'},check=True)
            data=json.loads(result.stdout)
            self.assertEqual(data['analysis_mode'],'coupled')
            self.assertFalse(any(x['title']=='绝热分析和塑性生热' for x in data['results']))
            review=subprocess.run([sys.executable,str(entry),'thermal-review','--inp',str(path),
                                   '--output-dir',str(Path(tmp)/'output')],capture_output=True,text=True,
                                  encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1'},check=True)
            data=json.loads(review.stdout)
            self.assertEqual(data['version'],'X1.4')
            self.assertTrue(Path(data['report']).is_file())
            self.assertTrue(data['inputs_unchanged'])

    def test_export_never_inside_source_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.setup_file(tmp);data=make_review(path)
            with self.assertRaises(ValueError):export_review(data,path.parent/'out')
            out=export_review(data,Path(tmp)/'results')
            self.assertTrue((out/'报告.md').is_file())
            self.assertEqual(path.read_text(),EXPLICIT)

    def test_journal_confines_paths_and_preserves_order(self):
        import x1.journal as journal
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(journal,'ROOT',Path(tmp)):
            add('history','记录一','用户说明',note='操作时间未核实')
            add('history','记录二','待核对')
            self.assertEqual([e['event'] for e in show('history')['events']],['记录一','记录二'])
            self.assertIn('不是操作执行时间',show('history')['events'][0]['time_note'])
            with self.assertRaises(ValueError):add('../bad','x','用户说明')


if __name__=='__main__':unittest.main()
