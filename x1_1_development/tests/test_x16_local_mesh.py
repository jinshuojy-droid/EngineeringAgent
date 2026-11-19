"""覆盖局部网格的区外保护、分带定位与体积失败明细。"""
from copy import deepcopy
import unittest

import numpy as np

from x1.mesh_cae_optimize import VolumeGuardError, verify_exports
from x1.mesh_local import TetMesh, compare_trial, find_cells, selector_labels


def sample():
    return {'ok': True, 'nodes': [[1, 0, 0, 0], [2, 0, 1, 0], [3, 0, 0, 1], [4, -1, 0, 0], [5, 1, 0, 0]],
            'elements': [[10, 'C3D4T', 1, 2, 3, 4], [20, 'C3D4T', 1, 3, 2, 5]],
            'warnings': [10, 20], 'errors': [], 'unavailable': [], 'instances': ['PART-1'],
            'cells': {'0': [10], '1': [20]}, 'incompatible_faces': [], 'seeds': {'0': [1], '1': [1]},
            'bindings': {'regions': {'assembly.surfaces.Contact': {'kind': 'geometry'}},
                         'references': {'constraints.Tool': {'ref': 'Ref'}}, 'unknown': []},
            'stable_time': {'worst': 1e-7}}


class LocalMeshTests(unittest.TestCase):
    def test_selection_identifies_cell_and_missing_labels(self):
        model = TetMesh(sample())
        chosen = selector_labels(model, {'kind': 'plane', 'value': [(0, -.25)], 'width': .2})
        self.assertEqual(chosen, {10})
        self.assertEqual(find_cells(model, chosen), ([0], {10}))
        with self.assertRaisesRegex(ValueError, '未知单元'):
            selector_labels(model, {'kind': 'elements', 'value': [99]})

    def test_connection_changes_inside_can_use_region_volume_comparison(self):
        before = sample()
        after = deepcopy(before)
        after['elements'][0][0] = 11
        after['cells']['0'] = [11]
        after['warnings'] = [20]
        after['seeds']['0'] = [.5]
        result = compare_trial(TetMesh(before), TetMesh(after), [0], [0], .05, 1e-8)
        self.assertFalse(result['protection_failures'])
        self.assertEqual(result['region_warning_reduction'], 1)
        self.assertEqual(result['outside']['interface_faces'], 1)
        self.assertEqual(result['region_total_volume_ratio'], 1)

    def test_outside_mesh_seed_and_binding_changes_are_rejected(self):
        before = sample()
        after = deepcopy(before)
        after['nodes'][4][1] += .1
        after['warnings'] = []
        after['seeds']['1'] = [.5]
        after['bindings']['references']['constraints.Tool']['ref'] = 'Other'
        result = compare_trial(TetMesh(before), TetMesh(after), [0], [0], .05, 1e-8)
        for rule in ('mesh_equal', 'warnings_equal', 'protected_seeds_equal', '模型区域或引用发生变化'):
            self.assertIn(rule, result['protection_failures'])

    def test_same_connectivity_volume_failure_includes_physical_values(self):
        model = {'ok': True, 'nodes': [[1, 0, 0, 0], [2, 1, 0, 0], [3, 0, 1, 0],
                                       [4, 0, 0, 1], [5, .2, .2, .2]],
                 'elements': [[10, 'C3D4T', 1, 2, 3, 5], [11, 'C3D4T', 1, 3, 4, 5],
                              [12, 'C3D4T', 1, 4, 2, 5], [13, 'C3D4T', 2, 4, 3, 5]],
                 'warnings': [10], 'errors': [], 'unavailable': [], 'instances': ['PART-1']}
        after = deepcopy(model)
        after['nodes'][4][3] = .02
        with self.assertRaises(VolumeGuardError) as error:
            verify_exports(model, after)
        failed = error.exception.failures[0]
        self.assertEqual(failed['label'], 10)
        self.assertEqual(list(failed['nodes']), [1, 2, 3, 5])
        self.assertTrue(np.isclose(failed['ratio'], .1))
