"""从 V2 只读导出中流式追踪指定单元，不推断实例的物理身份。"""
import csv
import json
import math
import os
from collections import defaultdict
from pathlib import Path


def same_odb(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def element_track(extracted, instance, label, geometry, odb_file=None):
    root = Path(extracted)
    entities, fields = root / 'entities.csv', root / 'field.csv'
    element, nodes = None, {}
    with entities.open(encoding='utf-8', newline='') as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            if odb_file and not same_odb(row.get('odb_file'), odb_file):
                raise ValueError('实体 CSV ODB 来源不一致，第 %d 行' % line)
            if row['instance'].upper() != instance.upper():
                continue
            if row['entity_type'] == 'element' and row['label'] == str(label):
                if element is not None:
                    raise ValueError('单元标签重复，不能确定单元身份')
                element = {'type': row['element_type'], 'connectivity': json.loads(row['connectivity_json']),
                           'source': {'file': str(entities), 'line': line}}
            elif row['entity_type'] == 'node':
                node = int(row['label'])
                if node in nodes:
                    raise ValueError('同一实例的节点标签重复，不能确定初始坐标')
                nodes[node] = (json.loads(row['coordinates_json']), line)
    if element is None:
        return {'instance': instance, 'element': label, 'status': '单元不在导出实体中', 'frames': []}
    connectivity = element['connectivity']
    if any(n not in nodes for n in connectivity):
        return {'instance': instance, 'element': label, 'element_definition': element,
                'status': '单元节点坐标不完整', 'frames': []}
    frames = defaultdict(lambda: {'u': {}, 'peeq': [], 'contact': []})
    with fields.open(encoding='utf-8', newline='') as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            if odb_file and not same_odb(row.get('odb_file'), odb_file):
                raise ValueError('场输出 CSV ODB 来源不一致，第 %d 行' % line)
            if row['instance'].upper() != instance.upper():
                continue
            is_u = row['variable'] == 'U' and row['node_label'] and int(row['node_label']) in connectivity
            is_p = row['variable'] == 'PEEQ' and row['element_label'] == str(label)
            is_contact = (row['variable'].upper().startswith(('CPRESS', 'COPEN', 'CSLIP', 'CDISP'))
                          and row['node_label'] and int(row['node_label']) in connectivity)
            if not (is_u or is_p or is_contact):
                continue
            key = (row['step'], int(row['frame_index']))
            entry = frames[key]
            try:
                step_time = float(row['step_time']) if row['step_time'] else None
                total_time = float(row['odb_total_time']) if row['odb_total_time'] else None
            except (ValueError, TypeError):
                step_time, total_time = None, None
            if 'domain' in entry and (entry['domain'] != row.get('domain', '') or entry['step_time'] != step_time):
                raise ValueError('同一帧的域或时间记录矛盾，不能安全关联')
            entry.update({'step': row['step'], 'domain': row.get('domain', ''), 'frame_index': int(row['frame_index']),
                          'step_time': step_time, 'total_time': total_time})
            evidence = {'file': str(fields), 'line': line, 'position': row['position'],
                        'integration_point': row['integration_point'], 'value_index': row['value_index']}
            values = json.loads(row['values_json']) if row['values_json'] else None
            if is_u:
                node = int(row['node_label'])
                try:
                    labels = json.loads(row.get('component_labels_json', 'null'))
                except (ValueError, TypeError):
                    labels = None
                entry['u'].setdefault(node, []).append((values, evidence, labels))
            elif is_p:
                entry['peeq'].append({'values': values, 'source': evidence})
            else:
                entry['contact'].append({'variable': row['variable'], 'node': int(row['node_label']),
                                         'face': row.get('face', ''), 'values': values,
                                         'component_labels': row.get('component_labels_json', ''), 'source': evidence})
    result = []
    initial = [nodes[n][0] for n in connectivity]
    valid_initial = all(isinstance(p, (tuple, list)) and len(p) == 3 and
                        all(isinstance(v, (float, int)) and math.isfinite(v) for v in p) for p in initial)
    for _, frame in sorted(frames.items(), key=lambda v: (v[1]['total_time'] is None,
                                                           v[1]['total_time'] or 0, v[0])):
        us = frame.pop('u')
        frame['displacements'] = []
        for n in connectivity:
            matches = us.get(n, [])
            if len(matches) != 1:
                frame['displacements'].append({'node': n, 'status': '缺失或重复', 'count': len(matches)})
                continue
            values, src, labels = matches[0]
            frame['displacements'].append({'node': n, 'value': values, 'source': src, 'component_labels': labels})
        if frame['domain'].upper() != 'TIME':
            frame['geometry'] = {'status': '非时间域或域未知，不重建物理几何'}
        elif not valid_initial:
            frame['geometry'] = {'status': '初始节点坐标缺失或无效'}
        elif any('value' not in x or not isinstance(x['value'], (list, tuple)) or len(x['value']) != 3 or
               x.get('component_labels') != ['U1', 'U2', 'U3'] or
               any(v is None or not isinstance(v, (float, int)) or not math.isfinite(v) for v in x['value'][:3])
               for x in frame['displacements']):
            frame['geometry'] = {'status': '节点位移缺失、重复、无效或分量标签未确认'}
        else:
            current = [[initial[i][j] + frame['displacements'][i]['value'][j] for j in range(3)]
                       for i in range(len(initial))]
            frame['geometry'] = geometry(element['type'], connectivity, initial, current)
        result.append(frame)
    return {'instance': instance, 'element': label, 'element_definition': element,
            'initial_nodes': [{'label': n, 'coordinates': nodes[n][0],
                               'source': {'file': str(entities), 'line': nodes[n][1]}} for n in connectivity],
            'status': '已追踪保存帧' if result else '无对应保存帧', 'frames': result}
