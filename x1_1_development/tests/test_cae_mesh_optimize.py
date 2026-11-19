"""CAE 四面体副本优化的边界、检查结果与异常处理。"""
import tempfile
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from x1.mesh_cae_optimize import prepare_edits, cae_mesh_optimize, verify_exports, original_export_for_candidate
from x1.core import digest
from x1.mesh_optimize import MIN_VOLUME_RATIO, tetra_metrics, flags, smooth


def sample():
    coords=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1],[.3,.3,.08]])
    conn=np.array([[0,1,2,4],[0,1,3,4],[0,2,3,4],[1,2,3,4]])
    _,q,lo,hi=tetra_metrics(coords[conn]);bad=flags(q,lo,hi)
    return {'ok':True,'nodes':[[i+1]+x.tolist() for i,x in enumerate(coords)],
            'elements':[[i+1,'C3D4T']+[int(v)+1 for v in x] for i,x in enumerate(conn)],
            'warnings':[i+1 for i,x in enumerate(bad) if x], 'errors':[], 'unavailable':[], 'instances':['HORN-1']}


class CaeMeshOptimizeTests(unittest.TestCase):
    def test_smoothing_enforces_original_volume_during_search(self):
        mesh = sample()
        xyz = np.array([node[1:] for node in mesh['nodes']])
        conn = np.array([element[2:] for element in mesh['elements']]) - 1
        initial = tetra_metrics(xyz[conn])[0]
        unconstrained = smooth(xyz, conn, {0, 1, 2, 3}, 20, 3)[0]
        final = tetra_metrics(unconstrained[conn])[0]
        shrinking = np.argmin(np.abs(final / initial))
        self.assertLess(abs(final[shrinking]), abs(initial[shrinking]))
        floor = (abs(initial[shrinking]) + abs(final[shrinking])) / 2
        original = initial.copy()
        original[shrinking] = np.sign(initial[shrinking]) * floor / MIN_VOLUME_RATIO
        constrained = smooth(xyz, conn, {0, 1, 2, 3}, 20, 3, reference_volumes=original)[0]
        guarded = tetra_metrics(constrained[conn])[0]
        self.assertTrue(np.all(guarded * original > 0))
        self.assertTrue(np.all(np.abs(guarded) >= MIN_VOLUME_RATIO * np.abs(original)))

    def test_warn_set_match_and_boundary_is_preserved(self):
        source=sample(); out=prepare_edits(source,20,3)
        self.assertGreater(len(out['edits']),0)
        self.assertEqual([x['label'] for x in out['edits']],[5])
        self.assertEqual(out['boundary_count'],4)
        self.assertEqual(out['independent_new'],0)
        self.assertLess(out['independent_after'],out['independent_before'])

    def test_fail_closed_on_unknown_or_mismatch(self):
        for mutate in ('warnings','errors','unavailable','instances','elements'):
            data=sample()
            if mutate=='warnings': data['warnings']=[]
            elif mutate=='errors': data['errors']=[1]
            elif mutate=='unavailable': data['unavailable']=[1]
            elif mutate=='instances': data['instances']=['A','B']
            else: data['elements'][0][1]='C3D10M'
            with self.assertRaises(ValueError): prepare_edits(data,20,3)

    def test_original_unchanged_and_no_candidate_on_export_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); src=root/'input'; src.mkdir(); path=src/'model.cae';path.write_bytes(b'original')
            with patch('x1.mesh_cae_optimize._run_worker',return_value={'ok':False,'error':'license denied'}):
                out=cae_mesh_optimize(path,'Model-1','Horn',output_dir=root/'reports')
            self.assertFalse(out['ok']);self.assertIsNone(out['candidate']);self.assertTrue(out['source_unchanged'])
            self.assertEqual(path.read_bytes(),b'original');self.assertTrue(Path(out['report']).is_file())

    def test_reopened_candidate_checks_boundary_and_connectivity(self):
        before=sample(); proposal=prepare_edits(before,20,3); after=copy.deepcopy(before)
        for item in proposal['edits']:
            next(n for n in after['nodes'] if n[0]==item['label'])[1:]=item['after']
        after['warnings']=[]
        check=verify_exports(before,after)
        self.assertEqual(check['boundary_nodes_moved'],0)
        altered=copy.deepcopy(after);altered['nodes'][0][1]+=0.01
        with self.assertRaises(ValueError): verify_exports(before,altered)
        altered=copy.deepcopy(after);altered['elements'][0][-1]=1
        with self.assertRaises(ValueError): verify_exports(before,altered)

    def test_chained_candidate_checks_original_baseline_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); original=root/'original.cae'; original.write_bytes(b'initial')
            run=root/'run'; (run/'apply').mkdir(parents=True); (run/'export').mkdir()
            candidate=run/'apply'/'model_candidate.cae'; candidate.write_bytes(b'candidate')
            (run/'export'/'model_working.cae').write_bytes(b'initial')
            (run/'export'/'worker_result.json').write_text(json.dumps(sample()),encoding='utf-8')
            record={'ok':True,'candidate':str(candidate),'candidate_sha256':digest(candidate),'input':str(original),'input_sha256':digest(original)}
            (run/'结果.json').write_text(json.dumps(record),encoding='utf-8')
            self.assertEqual(original_export_for_candidate(candidate)['instances'],['HORN-1'])
            candidate.write_bytes(b'changed')
            with self.assertRaises(ValueError): original_export_for_candidate(candidate)


if __name__=='__main__': unittest.main()
