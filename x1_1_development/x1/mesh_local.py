"""局部网格试验。控制选区、记录可追溯结果、独立验证保存的 CAE。"""
import csv
import hashlib
import json
import shutil
from collections import Counter
from itertools import combinations
from pathlib import Path

import numpy as np

from x1.core import digest, new_run
from x1.mesh_cae_optimize import _run_worker
from x1.mesh_optimize import flags, tetra_metrics


def write_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


class TetMesh:
    def __init__(self, record):
        self.nodes = {row[0]: tuple(row[1:]) for row in record['nodes']}
        self.elements = {row[0]: (row[1], tuple(row[2:])) for row in record['elements']}
        if (len(self.nodes) != len(record['nodes']) or len(self.elements) != len(record['elements'])
                or not self.elements or any(len(nodes) != 4 or kind.upper() not in ('C3D4', 'C3D4T')
                                            for kind, nodes in self.elements.values())):
            raise ValueError('需要编号唯一且非空的四节点四面体网格。')
        self.warnings = set(record['warnings'])
        self.record = record

    def points(self, labels):
        return np.asarray([[self.nodes[node] for node in self.elements[label][1]] for label in labels], dtype=float)

    def face_counts(self, labels):
        return Counter(face for label in labels for face in combinations(sorted(self.elements[label][1]), 3))

    def coverage(self, labels):
        points = self.points(labels).reshape(-1, 3)
        return [points.min(axis=0).tolist(), points.max(axis=0).tolist()]

    def region_digest(self, labels):
        elements = [(label, self.elements[label]) for label in sorted(labels)]
        nodes = {node for _, (_, connectivity) in elements for node in connectivity}
        payload = [elements, [(node, self.nodes[node]) for node in sorted(nodes)]]
        return hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode('ascii')).hexdigest()

    def geometric_elements(self, labels):
        return Counter((self.elements[label][0], tuple(sorted(self.nodes[node] for node in self.elements[label][1])))
                       for label in labels)

    def measures(self, labels):
        labels = sorted(labels)
        if not labels:
            return {'elements': 0, 'official_warnings': 0}
        points = self.points(labels)
        signed, quality, low, high = tetra_metrics(points)
        left, right = zip(*combinations(range(4), 2))
        distances = np.linalg.norm(points[:, left] - points[:, right], axis=2)
        minimum_position = np.unravel_index(distances.argmin(), distances.shape)[0]
        return {'elements': len(labels), 'official_warnings': len(self.warnings.intersection(labels)),
                'official_errors': len(set(self.record['errors']).intersection(labels)),
                'independent_warnings': int(flags(quality, low, high).sum()),
                'min_angle': float(low.min()), 'total_volume': float(np.abs(signed).sum()),
                'nonpositive_orientation': int((signed <= 0).sum()),
                'volume_min_p05_median': np.quantile(np.abs(signed), [0, .05, .5]).tolist(),
                'shortest_edge': float(distances.min()), 'shortest_edge_element': labels[minimum_position]}


def selector_labels(mesh, selector):
    labels = set(mesh.elements)
    if selector['kind'] == 'named':
        chosen = set(mesh.record.get('selected_labels', []))
    elif selector['kind'] == 'elements':
        chosen = set(selector['value'])
    else:
        ordered = sorted(labels)
        center = mesh.points(ordered).mean(axis=1)
        if selector['kind'] == 'bbox':
            bounds = selector['value']
            inside = ((center >= bounds[:3]) & (center <= bounds[3:])).all(axis=1)
        else:
            inside = np.zeros(len(center), dtype=bool)
            for axis, offset in selector['value']:
                inside |= abs(center[:, axis] - offset) <= selector['width'] / 2
        chosen = {label for label, keep in zip(ordered, inside) if keep}
    if not chosen or not chosen <= labels:
        raise ValueError('选区无有效单元，或含未知单元编号。')
    return chosen


def find_cells(mesh, chosen):
    cells = {int(cell): set(labels) for cell, labels in mesh.record['cells'].items()}
    hits = sorted(cell for cell, labels in cells.items() if chosen.intersection(labels))
    effective = set().union(*(cells[cell] for cell in hits))
    if chosen - effective:
        raise ValueError('部分选中单元未关联到 Abaqus 实体分区。')
    return hits, effective


def outside_check(before, after, old_out, new_out):
    old_hash, new_hash = before.region_digest(old_out), after.region_digest(new_out)
    return {'before_sha256': old_hash, 'after_sha256': new_hash,
            'mesh_equal': old_hash == new_hash,
            'outside_spatial_match': before.geometric_elements(old_out) == after.geometric_elements(new_out),
            'warnings_equal': before.warnings & old_out == after.warnings & new_out,
            'warning_labels_before': sorted(before.warnings & old_out),
            'warning_labels_after': sorted(after.warnings & new_out)}


def compare_trial(original, candidate, cell_ids, changed_edges, min_size, minimum_step):
    """区外逐编号核对，交界面按三角面三节点核对。"""
    old_target = {label for cell in cell_ids for label in original.record['cells'][str(cell)]}
    new_target = {label for cell in cell_ids for label in candidate.record['cells'][str(cell)]}
    old_out, new_out = set(original.elements) - old_target, set(candidate.elements) - new_target
    old_metrics, new_metrics = original.measures(old_target), candidate.measures(new_target)
    geometry = outside_check(original, candidate, old_out, new_out)
    old_interface = original.face_counts(old_out).keys() & original.face_counts(old_target).keys()
    new_interface = candidate.face_counts(new_out).keys() & candidate.face_counts(new_target).keys()
    geometry['interface_equal'] = old_interface == new_interface and not candidate.record['incompatible_faces']
    geometry['interface_faces'] = len(new_interface)
    geometry['interface_spatial_match'] = {
        tuple(sorted(original.nodes[node] for node in face)) for face in old_interface} == {
        tuple(sorted(candidate.nodes[node] for node in face)) for face in new_interface}
    old_seeds, new_seeds = original.record['seeds'], candidate.record['seeds']
    changed_edges = {str(edge) for edge in changed_edges}
    geometry['protected_seeds_equal'] = (old_seeds.keys() == new_seeds.keys() and all(
        seed == new_seeds[edge] for edge, seed in old_seeds.items() if edge not in changed_edges))
    geometry['geometry_vertices_equal'] = original.record.get('geometry_vertices') == candidate.record.get('geometry_vertices')
    left, right = original.record['bindings'], candidate.record['bindings']
    binding_changes = {section: sorted(key for key in left[section].keys() | right[section].keys()
                                       if left[section].get(key) != right[section].get(key))
                       for section in ('regions', 'references')}
    keyword_change = left.get('keywords') != right.get('keywords')
    reference_text = json.dumps([left['references'], right['references']], ensure_ascii=False)
    internal = [key for key in binding_changes['regions'] if '.allInternal' in key
                and json.dumps(key.split('.')[-1], ensure_ascii=False) not in reference_text
                and json.dumps(key.split('.', 2)[-1], ensure_ascii=False) not in reference_text]
    binding_changes['regions'] = [key for key in binding_changes['regions'] if key not in internal]
    unknown = sorted(set(left['unknown'] + right['unknown']))
    changed_types = {candidate.elements[label][0] for label in new_target} != {
        original.elements[label][0] for label in old_target}
    volume_ratio = new_metrics['total_volume'] / old_metrics['total_volume']
    step = candidate.record['stable_time'].get('worst')
    failures = [key for key, value in geometry.items() if key.endswith('_equal') and not value]
    for condition, message in (
            (changed_types, '试划区单元类型发生变化'),
            (original.record['errors'] or original.record['unavailable'], '基准网格存在错误或未检查单元'),
            (candidate.record['errors'] or candidate.record['unavailable'], '官方网格存在错误或未检查单元'),
            (any(binding_changes.values()), '模型区域或引用发生变化'),
            (keyword_change, '接触、约束、载荷或历史输出关键字发生变化'),
            (unknown, '存在尚未验证的模型引用'),
            (abs(volume_ratio - 1) > .005, '选区网格总体积偏差超过 0.5%'),
            (new_metrics['shortest_edge'] < min_size, '新网格最短边低于设置下限'),
            (step is None or step < minimum_step, '最小稳定时间增量低于门槛或未知')):
        if condition:
            failures.append(message)
    return {'outside': geometry, 'region_before': old_metrics, 'region_after': new_metrics,
            'region_total_volume_ratio': volume_ratio, 'binding_changes': binding_changes,
            'unreferenced_internal_region_changes': internal, 'binding_keywords_changed': keyword_change,
            'binding_unknown': unknown, 'stable_time': candidate.record['stable_time'],
            'official_before': len(original.warnings), 'official_after': len(candidate.warnings),
            'protection_failures': failures,
            'region_warning_reduction': (1 - new_metrics['official_warnings'] / old_metrics['official_warnings']
                                         if old_metrics['official_warnings'] else None)}


def warning_report(export, output_dir, bands, band_width, job):
    export = Path(export).resolve()
    record = load_json(export)
    mesh = TetMesh(record.get('mesh', record))
    root = new_run(Path(output_dir).resolve(), 'cae_mesh_locate')
    ordered = sorted(mesh.warnings)
    counts = Counter()
    csv_path = root / '警告单元.csv'
    boundary = {node for face, count in mesh.face_counts(mesh.elements).items() if count == 1 for node in face}
    with csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['单元', 'X形心', 'Y形心', 'Z形心', '最小二面角', '最大二面角', '外接球质量', '体积',
                         '固定边界节点数', '分带'])
        if ordered:
            points = mesh.points(ordered)
            volume, quality, low, high = tetra_metrics(points)
            for index, (label, center) in enumerate(zip(ordered, points.mean(axis=1))):
                groups = [f'{"XYZ"[axis]}={offset:g}' for axis, offset in bands
                          if abs(center[axis] - offset) <= band_width / 2] or ['其余']
                counts.update(groups)
                fixed = sum(node in boundary for node in mesh.elements[label][1])
                writer.writerow([label, *center, low[index], high[index], quality[index], abs(volume[index]), fixed,
                                 ' / '.join(groups)])
    result = {'ok': True, 'input': str(export), 'job': job, 'official_warnings': len(ordered), 'groups': dict(counts),
              'band_width': band_width, 'csv': str(csv_path), 'report': str(root / '报告.md')}
    write_json(root / '结果.json', result)
    lines = ['# 焊头网格警告定位', '', f'输入 `{export}`，作业 `{job}`。', '',
             f'官方警告 {len(ordered)} 个。{counts and "各分带见下表。" or ""}', '',
             '|分带|警告数|', '|---|---:|']
    lines.extend(f'|{name}|{count}|' for name, count in counts.items())
    lines += ['', '平面带按部件坐标和形心统计，可能重叠。', f'逐单元证据见 `{csv_path}` 第 2 行起。']
    (root / '报告.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def run_local(cae, model, part, selector, options, abaqus, output_dir):
    source = Path(cae).resolve()
    root = Path(output_dir).resolve()
    if source.suffix.lower() != '.cae' or not source.is_file():
        raise ValueError('请选择有效的 CAE 文件。')
    if root == source.parent or source.parent in root.parents:
        raise ValueError('报告根目录应位于输入 CAE 所在目录之外。')
    initial_hash = digest(source)
    options = dict(options)
    resume = options.pop('resume')
    plan = {'input': str(source), 'input_sha256': initial_hash, 'model': model, 'part': part,
            'selector': selector, 'options': options, 'abaqus': abaqus}
    if resume:
        folder = Path(resume).resolve()
        if root not in folder.parents:
            raise ValueError('续跑路径应位于报告根目录。')
        result = load_json(folder / '结果.json')
        if result['plan'] != json.loads(json.dumps(plan)):
            raise ValueError('续跑参数或输入哈希与原试验不一致。')
        result.update(ok=False, candidate=None, status='试验续跑中')
        result.pop('error', None)
    else:
        folder = new_run(root, 'cae_mesh_local')
        result = {'ok': False, 'version': 'X1.6', 'input': str(source), 'job': '未启动作业',
                  'run_dir': str(folder), 'report': str(folder / '报告.md'), 'candidate': None,
                  'plan': plan, 'trials': {}, 'worker_runs': {}, 'status': '试验进行中'}
    write_json(folder / '结果.json', result)

    def run_stage(name, mode, input_path, **data):
        stage = folder / name
        if stage.exists():
            stage = new_run(folder, name)
        else:
            stage.mkdir()
        copy_path = stage / 'model_working.cae'
        stage_input_hash = digest(input_path)
        shutil.copy2(input_path, copy_path)
        if digest(copy_path) != stage_input_hash:
            raise RuntimeError('工作副本与指定输入的哈希不一致。')
        request = {'model_copy': str(copy_path), 'model': model, 'part': part, 'mode': mode, **data}
        worker = _run_worker(abaqus, stage, 'worker_local_mesh.py', request, recover_completed=True)
        result['worker_runs'][name] = {'returncode': worker['returncode'], 'abnormal_exit': worker['abnormal_exit'],
                                       'input_sha256': stage_input_hash,
                                       'log': str(stage / '后台日志.txt'), 'data': str(stage / 'worker_result.json')}
        return worker, copy_path

    try:
        worker, _ = run_stage('baseline', 'inspect', source, selector=selector)
        if not worker.get('ok'):
            raise RuntimeError(worker.get('error') or 'Abaqus 内核未返回完整基准结果。')
        base = TetMesh(worker['mesh'])
        selected = selector_labels(base, selector)
        cells, effective = find_cells(base, selected)
        result['selection'] = {'target_elements': len(selected), 'target_warnings': len(base.warnings & selected),
                               'cell_ids': cells, 'effective_elements': len(effective),
                               'effective_warnings': len(base.warnings & effective), 'effective_bounds': base.coverage(effective),
                               'outside_elements': len(base.elements) - len(effective)}
        if options['partition']:
            probe, _ = run_stage('partition_probe', 'probe', source, partition=options['partition'])
            if not probe.get('ok'):
                raise RuntimeError(probe.get('error') or '分区探测失败。')
            observation = probe['partition_probe']
            missing = (set(base.elements) - selected) - set(observation.pop('remaining_elements'))
            missing_nodes = set(base.nodes) - set(observation.pop('remaining_nodes'))
            result['partition_probe'] = {**observation, 'outside_missing': len(missing),
                                         'outside_missing_examples': sorted(missing)[:20], 'mesh_nodes_missing': len(missing_nodes)}
        if options['preview']:
            result.update(ok=True, status='选区预览完成', exit_code=0)
        elif effective == set(base.elements) or (effective != selected and not options['accept_expansion']):
            result.update(status='缺少对应选区的独立实体分区，未执行整件重划', exit_code=3)
        elif not options['sizes']:
            raise ValueError('正式试划需要提供 --seed-size。')
        else:
            for index, size in enumerate(options['sizes'], 1):
                name = f'trial_{index:02d}'
                if name in result['trials'] and 'error' not in result['trials'][name]:
                    continue
                applied, work = run_stage(name, 'apply', source, cell_ids=cells, seed_size=size,
                                         algorithm=options['algorithm'], bias=options['bias'], bias_axis=options['bias_axis'])
                outcome = {'seed_size': size, 'candidate': None}
                if applied.get('ok'):
                    candidate_hash = digest(work)
                    reopened, _ = run_stage(name + '_verify', 'inspect', work)
                    if reopened.get('ok') and digest(work) == candidate_hash and reopened['mesh'] == applied['mesh']:
                        try:
                            outcome.update(compare_trial(base, TetMesh(reopened['mesh']), cells,
                                                         applied['editable_edge_ids'], options['min_size'], options['min_step']))
                        except (ValueError, KeyError) as exc:
                            outcome['protection_failures'] = [str(exc)]
                        reduction = outcome.get('region_warning_reduction')
                        if (not outcome['protection_failures'] and reduction is not None and reduction > 0
                                and reduction >= options['min_warning_reduction']):
                            outcome.update(candidate=str(work), candidate_sha256=candidate_hash)
                    else:
                        outcome['error'] = reopened.get('error') or '未完成独立重开核验。'
                else:
                    outcome['error'] = applied.get('error') or '试划未完成。'
                result['trials'][name] = outcome
                write_json(folder / '结果.json', result)
            eligible = [item for item in result['trials'].values() if item.get('candidate') and
                        Path(item['candidate']).is_file() and digest(item['candidate']) == item['candidate_sha256']]
            if eligible:
                best = min(eligible, key=lambda item: (item['official_after'], -item['stable_time']['worst']))
                result.update(ok=True, candidate=best['candidate'], status='候选已完成网格与可读绑定复核', exit_code=0)
            elif any(item.get('protection_failures') for item in result['trials'].values()):
                result.update(status='保护检查未通过', exit_code=3)
            elif all(item.get('error') for item in result['trials'].values()):
                result.update(status='试划或 Abaqus 重开失败', exit_code=4)
            else:
                result.update(status='警告改善未达到候选发布门槛', exit_code=2)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        result.update(ok=False, error=str(exc), status='试验未完成', exit_code=4 if isinstance(exc, RuntimeError) else 3)
    result['source_unchanged'] = source.is_file() and digest(source) == initial_hash
    if not result['source_unchanged']:
        result.update(ok=False, candidate=None, status='输入 CAE 哈希发生变化', exit_code=3)
    write_json(folder / '结果.json', result)
    report = ['# 焊头局部网格对照', '', f'输入 `{source}`，模型 `{model}`，部件 `{part}`，作业未启动。', '',
              result['status'], '', f'源 CAE 哈希一致 `{result["source_unchanged"]}`。']
    if 'selection' in result:
        area = result['selection']
        report += ['', f'预选 {area["target_elements"]} 单元及 {area["target_warnings"]} 个警告。'
                   f'对应实体区含 {area["effective_elements"]} 单元。']
    if 'partition_probe' in result:
        probe = result['partition_probe']
        report += ['', f'探测分区数 {probe["cells_before"]} → {probe["cells_after"]}。'
                   f'预选区外有 {probe["outside_missing"]} 个原单元编号未保留。']
    protection_names = {'mesh_equal': '区外网格编号、坐标或连接', 'warnings_equal': '区外警告标签',
                        'interface_equal': '交界面连接或编号', 'protected_seeds_equal': '受保护的边布种',
                        'geometry_vertices_equal': '部件几何顶点'}
    for name, trial in result['trials'].items():
        report += ['', f'- {name}，目标布种 {trial["seed_size"]}。'
                   f'警告 {trial.get("official_before", "未知")} → {trial.get("official_after", "未知")}。'
                   f'复核记录见 `结果.json` 的 `trials.{name}`。']
        if trial.get('protection_failures'):
            reasons = '、'.join(protection_names.get(item, item) for item in trial['protection_failures'])
            report.append(f'  保护检查未通过项为 {reasons}。')
        if trial.get('error'):
            report.append(f'  内核或重开错误见 `结果.json` 的 `trials.{name}.error`。')
    if result.get('error'):
        report += ['', result['error']]
    report += ['', '数据源为同目录 `结果.json` 的 `selection`、`partition_probe`、`trials` 和 `worker_runs`。']
    (folder / '报告.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    return result
