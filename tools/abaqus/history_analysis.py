"""结果整理：以 CSV 行号和 ODB 坐标保留证据，不推测物理结论。"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path


def read_history(path):
    groups = defaultdict(list)
    with Path(path).open(encoding='utf-8', newline='') as f:
        for line, row in enumerate(csv.DictReader(f), 2):
            row['csv_line'] = line
            for name in ('step_time', 'odb_total_time', 'value'):
                try:
                    row[name] = float(row[name]) if row[name] else None
                except ValueError:
                    row[name] = None
            row['segment'] = int(row['segment'])
            row['sample_index'] = int(row['sample_index'])
            groups[(row['step'], row['history_region'], row['variable'])].append(row)
    return groups


def evidence(row):
    return {k: row[k] for k in ('odb_file', 'step', 'history_region', 'variable',
        'sample_index', 'segment', 'step_time', 'odb_total_time', 'value', 'csv_line')}


def valid(rows):
    return [r for r in rows if r['value'] is not None and math.isfinite(r['value']) and r['step_time'] is not None]


def stats(rows):
    rows = valid(rows)
    if not rows:
        return None
    return {'count': len(rows), 'first': evidence(rows[0]), 'last': evidence(rows[-1]),
            'minimum': evidence(min(rows, key=lambda r: r['value'])),
            'maximum': evidence(max(rows, key=lambda r: r['value'])),
            'segments': sorted(set(r['segment'] for r in rows))}


def retreat(rows):
    # 每个原始段独立计算，避免从不同重启片段中拼接峰谷。
    candidates = []
    for segment in sorted({r['segment'] for r in rows}):
        points = valid([r for r in rows if r['segment'] == segment])
        if not points:
            continue
        peak = points[0]
        for row in points[1:]:
            if row['value'] > peak['value']:
                peak = row
            else:
                candidates.append((peak['value'] - row['value'], peak, row))
    if not candidates:
        return None
    amount, high, low = max(candidates, key=lambda x: x[0])
    return {'drop': amount, 'earlier': evidence(high), 'later': evidence(low),
            'meaning': '同一原始记录段内 U3 数值下降；未判定物理方向或原因'}


def energy_ratios(groups, step, region='Assembly ASSEMBLY'):
    ae = valid(groups.get((step, region, 'ALLAE'), []))
    ie = valid(groups.get((step, region, 'ALLIE'), []))
    # 只对同段且相同时间的唯一记录相除；不同频率无需序号相同，不插值。
    ae_map, ie_map = defaultdict(list), defaultdict(list)
    for r in ae:
        ae_map[(r['segment'], r['step_time'])].append(r)
    for r in ie:
        ie_map[(r['segment'], r['step_time'])].append(r)
    pairs = []
    for a in ae:
        key = (a['segment'], a['step_time'])
        candidates = ie_map.get(key, [])
        b = candidates[0] if len(candidates) == 1 and len(ae_map[key]) == 1 else None
        if b is not None and abs(b['value']) > 1e-12:
            pairs.append({'ratio': a['value'] / b['value'], 'allae': evidence(a), 'allie': evidence(b)})
    return {'count': len(pairs), 'last': pairs[-1] if pairs else None, 'samples': pairs,
            'max': max(pairs, key=lambda p:p['ratio']) if pairs else None,
            'method': '同区域、同原始段、相同时间且该时间在两个序列均唯一；内能绝对值大于 1e-12；不插值'}


def field_summaries(path):
    # 接触输出按每一实际场变量和帧分别统计；始终保留原始变量名称。
    groups = {}
    missing = 0
    with Path(path).open(encoding='utf-8', newline='') as f:
        for line, row in enumerate(csv.DictReader(f), 2):
            try:
                values = json.loads(row['values_json'])
            except (ValueError, TypeError):
                values = None
            if values is None or any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                missing += 1
                continue
            name = row['variable']
            if not name.upper().startswith(('CPRESS', 'COPEN', 'CSLIP', 'CDISP')):
                continue
            if not values:
                continue
            try:
                step_time = float(row['step_time'])
            except (TypeError, ValueError):
                missing += 1
                continue
            if not math.isfinite(step_time):
                missing += 1
                continue
            key = (row['step'], int(row['frame_index']), name)
            if key not in groups:
                groups[key] = {'step': row['step'], 'frame_index': int(row['frame_index']),
                    'step_time': step_time, 'variable': name, 'count': 0,
                    'min_component': None, 'max_component': None, 'min_line': None, 'max_line': None,
                    'negative_components': 0}
            g = groups[key]
            g['count'] += 1
            for value in values:
                if value < 0:
                    g['negative_components'] += 1
                if g['min_component'] is None or value < g['min_component']:
                    g['min_component'], g['min_line'] = value, line
                if g['max_component'] is None or value > g['max_component']:
                    g['max_component'], g['max_line'] = value, line
    return list(groups.values()), missing


def build_diagnosis(meta, groups, contact, invalid_count, rp_region=None, preload_step=None,
                    expected_min=None, expected_max=None, expected_direction=None,
                    energy_ratio_limit=None, preload_ke_ie_limit=None):
    result = {'facts': [], 'rules': [], 'engineering_questions': [], 'steps': {},
              'reference_point_selection': rp_region, 'units': meta['units'], 'invalid_field_values': invalid_count}
    invalid_history = sum(1 for rows in groups.values() for r in rows
                          if r['value'] is None or r['step_time'] is None or not math.isfinite(r['value']) or not math.isfinite(r['step_time']))
    result['invalid_history_values'] = invalid_history
    selected = [s for s in meta['steps'] if s['extracted']]
    for s in selected:
        step = s['name']
        available = sorted({v for outputs in s['history_regions'].values() for v in outputs})
        requested = ('U1','U2','U3','V1','V2','V3','ALLKE','ALLIE','ALLAE','ETOTAL','RF1','RF2','RF3')
        result['steps'][step] = {'domain': s['domain'], 'available_history': available,
            'missing_requested_history': [v for v in requested if v not in available],
            'available_fields': s['field_variables'],
            'missing_requested_fields': [p for p in ('U','S','PEEQ','CPRESS','COPEN','CSLIP','CDISP')
                                         if not any(v == p or v.startswith(p+' ') or (p in ('CSLIP','CDISP') and v.startswith(p)) for v in s['field_variables'])],
            'energy_ratio': energy_ratios(groups, step), 'reference_displacement': {},
            'last_saved_frame': s['last_saved_frame_value'], 'declared_time_period': s['declared_time_period']}
        if rp_region:
            for var in ('U1','U2','U3','V1','V2','V3','RF1','RF2','RF3'):
                rows = groups.get((step, rp_region, var), [])
                result['steps'][step]['reference_displacement'][var] = stats(rows)
            r = retreat(groups.get((step, rp_region, 'U3'), []))
            result['steps'][step]['u3_recorded_decrease'] = r
            if r:
                result['facts'].append({'type':'u3_decrease', 'step':step, **r})
            u3 = result['steps'][step]['reference_displacement']['U3']
            if u3 and (expected_min is not None or expected_max is not None):
                outside = ((expected_min is not None and u3['minimum']['value'] < expected_min) or
                           (expected_max is not None and u3['maximum']['value'] > expected_max))
                result['rules'].append({'type':'user_u3_range', 'step':step, 'min':expected_min,
                                        'max':expected_max, 'outside':outside, 'evidence':u3})
            if expected_direction:
                rows = valid(groups.get((step, rp_region, 'U3'), []))
                if rows:
                    seg = rows[-1]['segment']
                    last_segment = [r for r in rows if r['segment'] == seg]
                    first, last = last_segment[0], last_segment[-1]
                    delta = last['value'] - first['value']
                    opposite = delta < 0 if expected_direction == 'positive' else delta > 0
                    result['rules'].append({'type':'user_u3_direction', 'step':step, 'expected':expected_direction,
                        'comparison':'最后记录段末值减首值；不代表周期内每点方向', 'delta':delta,
                        'opposite':opposite, 'first':evidence(first), 'last':evidence(last)})
        ratios = result['steps'][step]['energy_ratio']
        if ratios['last']:
            result['facts'].append({'type':'energy_ratio', 'step':step, **ratios['last']})
        if energy_ratio_limit is not None and ratios['max']:
            result['rules'].append({'type':'user_energy_ratio_limit', 'step':step, 'limit':energy_ratio_limit,
                'exceeded':ratios['max']['ratio'] > energy_ratio_limit, 'evidence':ratios['max'],
                'note':'仅比较用户设定阈值，不构成物理可靠性判定。'})
    result['contact_frames'] = contact
    if not rp_region:
        result['engineering_questions'].append('未指定参考点历史区域，不推断哪个节点是焊头参考点。')
    if expected_min is None and expected_max is None:
        result['engineering_questions'].append('未提供位移正常范围和单位，不能自动判定幅值异常。')
    if expected_direction is None:
        result['engineering_questions'].append('未提供位移期望方向，不能自动判定方向异常。')
    if not preload_step:
        result['engineering_questions'].append('未指定预压分析步，不判断预压末动能残留。')
    elif preload_step not in result['steps']:
        result['engineering_questions'].append('指定预压步未提取或不存在。')
    else:
        for var in ('ALLKE','ALLIE'):
            result['steps'][preload_step]['preload_'+var] = stats(groups.get((preload_step, 'Assembly ASSEMBLY', var), []))
        ke = result['steps'][preload_step]['preload_ALLKE']
        ie = result['steps'][preload_step]['preload_ALLIE']
        if ke and ie:
            a, b = ke['last'], ie['last']
            if (a['segment'], a['step_time']) == (b['segment'], b['step_time']) and abs(b['value']) > 1e-12:
                result['steps'][preload_step]['preload_ke_ie'] = {'ratio': a['value']/b['value'], 'allke': a, 'allie': b}
                if preload_ke_ie_limit is not None:
                    result['rules'].append({'type':'user_preload_ke_ie_limit', 'step':preload_step,
                        'limit':preload_ke_ie_limit, 'exceeded':a['value']/b['value'] > preload_ke_ie_limit,
                        'allke':a, 'allie':b, 'note':'仅比较用户设定阈值，不作为普适准静态判据。'})
        result['engineering_questions'].append('预压末动能需结合加载速率、动能时间变化和问题类型判断，不套用通用阈值。')
    if invalid_count:
        result['rules'].append({'type':'nonfinite_or_unreadable_field_values', 'count':invalid_count,
                                'note':'可能为非有限值或不可读取，需检查 field.csv 中空值；不是自动断定物理错误。'})
    if invalid_history:
        result['rules'].append({'type':'nonfinite_or_unreadable_history_values', 'count':invalid_history,
                                'note':'检查 history.csv 中空值；不作物理原因推断。'})
    if not contact:
        result['engineering_questions'].append('提取结果没有可用接触场输出；不代表接触不存在。')
    elif any(x['negative_components'] for x in contact if x['variable'].upper().startswith('CPRESS')):
        first = next(x for x in contact if x['variable'].upper().startswith('CPRESS') and x['negative_components'])
        result['rules'].append({'type':'negative_contact_pressure', 'evidence':first,
                                'note':'检测到负的接触压力分量；需结合输出定义和符号约定核实。'})
    result['engineering_questions'].extend(['采样稀疏、重复或时间回退会影响峰值和连续性判断。',
        '能量比例只作数据提示，不能单独证明焊接成功或结果可信。'])
    if energy_ratio_limit is None:
        result['engineering_questions'].append('未提供人工能量比阈值，只报告比值及其变化，不自动认定是否超限。')
    return result
