"""X1.4：四面体质量、警告来源、保护机制和副本试验。"""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from x1.mesh_audit import tet_geometry, mesh_audit, read_inp, boundary_inventory
from x1.mesh_study import candidate_plan, mesh_study
from x1.mesh_optimize import tetra_metrics, flags, smooth, mesh_optimize


INP = '''*Part, name=Horn
*Node
1,0,0,0
2,1,0,0
3,0.5,0.866025403784439,0
4,0.5,0.288675134594813,0.816496580927726
*Element, type=C3D4T
1,1,2,3,4
*End Part
*Assembly, name=Assembly
*Instance, name=Horn-1, part=Horn
*End Instance
*End Assembly
'''
DAT = ''' Distorted tetrahedral elements
        Element         Quality measure     Min angle        Max angle     Adjusted nodes
        HORN-1.1           1.0          70.52878          70.52878       NO
'''


class Mesh14Tests(unittest.TestCase):
    def test_regular_tetrahedron_quality_and_angle(self):
        p=[[0,0,0],[1,0,0],[.5,math.sqrt(3)/2,0],[.5,math.sqrt(3)/6,math.sqrt(2/3)]]
        quality=tet_geometry(p)
        self.assertAlmostEqual(quality['quality'],1.0,places=10)
        self.assertAlmostEqual(quality['min_dihedral'],math.degrees(math.acos(1/3)),places=8)
        self.assertAlmostEqual(quality['max_dihedral'],math.degrees(math.acos(1/3)),places=8)

    def test_degenerate_tetrahedron_has_unknown_angles(self):
        result=tet_geometry([[0,0,0],[1,0,0],[2,0,0],[3,0,0]])
        self.assertEqual(result['volume'],0)
        self.assertIsNone(result['quality'])
        self.assertIsNone(result['min_dihedral'])

    def test_quality_invariant_under_large_translation(self):
        pts=np.array([[0.,0,0],[1,0,0],[.5,math.sqrt(3)/2,0],[.5,math.sqrt(3)/6,math.sqrt(2/3)]])
        translated=pts+np.array([1e8,-1e8,1e8])
        self.assertAlmostEqual(tet_geometry(pts)['quality'],tet_geometry(translated)['quality'],places=6)
        self.assertAlmostEqual(tetra_metrics(pts[None])[1][0],tetra_metrics(translated[None])[1][0],places=6)

    def test_real_line_provenance_and_unchanged_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); inputs=root/'input'; inputs.mkdir(); out=root/'reports'
            inp=inputs/'job.inp'; inp.write_text(INP,encoding='ascii')
            dat=inputs/'job.dat'; dat.write_text(DAT,encoding='ascii')
            result=mesh_audit(inp,dat,'Horn',out)
            self.assertEqual(result['selected_mapped'],1)
            self.assertEqual(result['tet_quality_summary']['min_dihedral_below_10'],0)
            self.assertTrue(result['source_unchanged'])
            self.assertEqual(inp.read_text(),INP); self.assertEqual(dat.read_text(),DAT)
            self.assertIn('未验证',result['matching_status'])
            text=Path(result['csv']).read_text(encoding='utf-8-sig')
            self.assertIn('HORN-1,Horn,1,C3D4T',text)
            self.assertIn(',8,3,NO,',text)
            self.assertTrue(Path(result['report']).exists())

    def test_include_and_same_source_directory_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); inp=root/'job.inp'; dat=root/'job.dat'
            inp.write_text(INP,encoding='ascii'); dat.write_text(DAT,encoding='ascii')
            with self.assertRaises(ValueError): mesh_audit(inp,dat,output_dir=root)
            inp.write_text(INP+'*Include, input=mesh.inp\n',encoding='ascii')
            with self.assertRaises(ValueError): read_inp(inp)

    def test_unsupported_mesh_never_gets_complete_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'mixed.inp'
            path.write_text(INP.replace('*End Part','*Element, type=C3D10T\n2, 1,2,3,4,1,2,3,4,1,2\n*End Part'),encoding='ascii')
            parts,_,_=read_inp(path)
            self.assertIsNone(boundary_inventory(parts['HORN']))
            path.write_text(INP.replace('*Node','*Node, system=C'),encoding='ascii')
            with self.assertRaises(ValueError): read_inp(path)

    def test_mesh_study_aborts_safely_on_license_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); inputs=root/'input'; inputs.mkdir(); source=inputs/'model.cae'; source.write_bytes(b'original')
            with patch('x1.mesh_study.subprocess.run') as run:
                run.return_value.returncode=1; run.return_value.stdout='FlexNet -63,403'
                result=mesh_study(source,'Model-1','Horn','abaqus',root/'out')
            self.assertFalse(result['ok']); self.assertEqual(len(result['trials']),1)
            self.assertEqual(result['trials'][0]['trial']['name'],'baseline')
            self.assertIn('FlexNet',result['trials'][0]['error'])
            self.assertTrue(result['source_unchanged']); self.assertEqual(source.read_bytes(),b'original')
            self.assertTrue(Path(result['report']).exists())

    def test_study_plan_is_single_factor_and_bounded(self):
        plan=candidate_plan()
        self.assertEqual([p['kind'] for p in plan],['baseline','size','size','mapped_off','alternate'])
        self.assertNotIn('geometry',json.dumps(plan))

    def test_smoother_preserves_boundary_and_prevents_new_warnings(self):
        points=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1],[.3,.3,.08]])
        cells=np.array([[0,1,2,4],[0,1,3,4],[0,2,3,4],[1,2,3,4]])
        before_metrics=tetra_metrics(points[cells])
        result,moves,before,after,rounds=smooth(points,cells,{0,1,2,3},20,3)
        self.assertTrue(moves); self.assertLess(after.sum(),before.sum())
        self.assertTrue(np.array_equal(result[:4],points[:4]))
        self.assertFalse(np.any(after & ~before))
        self.assertTrue(np.all(tetra_metrics(result[cells])[0]*before_metrics[0]>0))

    def test_experimental_candidate_and_mismatched_dat(self):
        points=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1],[.3,.3,.08]])
        cells=np.array([[0,1,2,4],[0,1,3,4],[0,2,3,4],[1,2,3,4]])
        _,q,lo,hi=tetra_metrics(points[cells]); warnings=flags(q,lo,hi)
        node_rows=''.join('%s,%.12g,%.12g,%.12g\n'%((i+1,)+tuple(p)) for i,p in enumerate(points))
        element_rows=''.join('%s,%s\n'%(i+1,','.join(str(n+1) for n in cell)) for i,cell in enumerate(cells))
        source_text='*Part, name=Horn\n*Node\n'+node_rows+'*Element, type=C3D4T\n'+element_rows+'*End Part\n*Assembly, name=A\n*Instance, name=Horn-1, part=Horn\n*End Instance\n*End Assembly\n'
        dat_text='Distorted tetrahedral elements\n'+''.join('HORN-1.%s %.12g %.12g %.12g NO\n'%(i+1,q[i],lo[i],hi[i]) for i in range(len(cells)) if warnings[i])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); src=root/'input'; src.mkdir(); inp=src/'job.inp'; dat=src/'job.dat'
            inp.write_text(source_text,encoding='ascii'); dat.write_text(dat_text,encoding='ascii')
            result=mesh_optimize(inp,dat,'Horn',20,root/'out',3)
            self.assertTrue(result['source_unchanged']); self.assertTrue(result['candidate_inp'])
            self.assertLess(result['independent_warning_after'],result['independent_warning_before'])
            self.assertEqual(result['newly_flagged_elements'],0)
            self.assertEqual(inp.read_text(),source_text); self.assertEqual(dat.read_text(),dat_text)
            original,_,_=read_inp(inp); candidate,_,_=read_inp(Path(result['candidate_inp']))
            for n in (1,2,3,4): self.assertEqual(original['HORN']['nodes'][n][0],candidate['HORN']['nodes'][n][0])
            self.assertEqual(original['HORN']['elements'],candidate['HORN']['elements'])
            dat.write_text(dat_text.replace('NO','YES'),encoding='ascii')
            with self.assertRaises(ValueError): mesh_optimize(inp,dat,'Horn',20,root/'out',1)


if __name__=='__main__': unittest.main()
