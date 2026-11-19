"""Abaqus Python 只读提取：原始历程、场数据和可追溯清单。"""
import argparse
import csv
import json
import math
import os
import sys

HISTORY_PREFIXES = ('U1', 'U2', 'U3', 'V1', 'V2', 'V3', 'RF1', 'RF2', 'RF3',
                    'ALLKE', 'ALLIE', 'ALLAE', 'ETOTAL', 'CPRESS', 'COPEN',
                    'CSLIP', 'CDISP', 'CFNM')
FIELD_PREFIXES = ('U', 'S', 'PEEQ', 'CPRESS', 'COPEN', 'CSLIP', 'CDISP')


def matches(name, prefixes):
    upper = name.upper()
    return any(upper == p or upper.startswith(p + ' ') or
               (p in ('CSLIP', 'CDISP') and upper.startswith(p)) for p in prefixes)


def safe_attr(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def components(value):
    # 双精度 ODB 的 data 可能抛异常；优先使用实际可访问的数据。
    raw = safe_attr(value, 'dataDouble')
    if raw is None:
        raw = safe_attr(value, 'data')
    if raw is None:
        return None
    try:
        return [number(x) for x in raw]
    except TypeError:
        return [number(raw)]


def inventory_sets(repository):
    return sorted(str(k) for k in repository.keys())


def within(path, root):
    """跨盘路径没有共同父目录；这种情况不属于 root。"""
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root)
    except ValueError:
        return False


def extract(odb_path, output, step_filter=None, history_filter=None):
    from odbAccess import openOdb
    project = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))
    target = os.path.realpath(output)
    source_dir = os.path.realpath(os.path.dirname(os.path.abspath(odb_path)))
    if not within(target, project):
        raise ValueError('输出必须位于 EngineeringAgent 项目目录')
    if within(target, source_dir):
        raise ValueError('输出不能位于 ODB 原目录或其子目录')
    if os.path.exists(target):
        raise ValueError('输出目录已存在，拒绝覆盖')
    odb = openOdb(path=odb_path, readOnly=True)
    try:
        assembly = odb.rootAssembly
        meta = {'schema_version': '2.0', 'odb_path': os.path.abspath(odb_path),
                'read_only': True, 'units': 'ODB 不记录完整单位体系；未知，需用户确认',
                'instances': {}, 'assembly_sets': {}, 'steps': [], 'warnings': [],
                'selection': {'steps': step_filter or [], 'history_regions': history_filter or []}}
        for name, instance in assembly.instances.items():
            meta['instances'][name] = {'node_count': len(instance.nodes), 'element_count': len(instance.elements),
                'node_sets': inventory_sets(instance.nodeSets), 'element_sets': inventory_sets(instance.elementSets),
                'surfaces': inventory_sets(instance.surfaces)}
        for key, attr in [('node_sets', 'nodeSets'), ('element_sets', 'elementSets'),
                          ('surfaces', 'surfaces'), ('reference_points', 'referencePoints')]:
            repo = safe_attr(assembly, attr)
            meta['assembly_sets'][key] = inventory_sets(repo) if repo is not None else None
        meta['assembly_node_set_members'] = {}
        for set_name, node_set in assembly.nodeSets.items():
            groups = safe_attr(node_set, 'nodes', ())
            names = safe_attr(node_set, 'instanceNames', ())
            members = []
            for i, group in enumerate(groups):
                nodes = [group] if safe_attr(group, 'label') is not None else group
                instance_name = names[i] if i < len(names) else ''
                members.append({'instance': str(instance_name), 'node_labels': [int(n.label) for n in nodes]})
            meta['assembly_node_set_members'][set_name] = members
        meta['reference_point_candidates'] = {
            'method': '仅按集合名称查找参考点候选；不能自动确认物理角色',
            'node_sets': [x for x in meta['assembly_sets']['node_sets'] if 'REFERENCE_POINT' in x.upper() or x.upper().startswith('RP')],
            'api_reference_points': meta['assembly_sets']['reference_points']}
        known_steps = set(odb.steps.keys())
        unknown = set(step_filter or []) - known_steps
        if unknown:
            raise ValueError('找不到指定分析步: ' + ', '.join(sorted(unknown)))
        if history_filter:
            known_regions = set()
            for selected_name, selected_step in odb.steps.items():
                if not step_filter or selected_name in step_filter:
                    known_regions.update(selected_step.historyRegions.keys())
            unknown_regions = set(history_filter) - known_regions
            if unknown_regions:
                raise ValueError('所选分析步没有指定历史区域: ' + ', '.join(sorted(unknown_regions)))
        os.makedirs(output)
        with open(os.path.join(output, 'entities.csv'), 'w', newline='', encoding='utf-8') as ef:
            ew = csv.writer(ef)
            ew.writerow(['odb_file', 'instance', 'entity_type', 'label', 'coordinates_json', 'element_type', 'connectivity_json'])
            for inst_name, inst in assembly.instances.items():
                for node in inst.nodes:
                    ew.writerow([meta['odb_path'], inst_name, 'node', node.label, json.dumps([number(v) for v in node.coordinates]), '', ''])
                for element in inst.elements:
                    ew.writerow([meta['odb_path'], inst_name, 'element', element.label, '',
                                 str(element.type), json.dumps([int(v) for v in element.connectivity])])
        history_path, field_path = os.path.join(output, 'history.csv'), os.path.join(output, 'field.csv')
        with open(history_path, 'w', newline='', encoding='utf-8') as hf, open(field_path, 'w', newline='', encoding='utf-8') as ff, \
             open(os.path.join(output, 'history.json'), 'w', encoding='utf-8') as hj, \
             open(os.path.join(output, 'field.json'), 'w', encoding='utf-8') as fj:
            hw, fw = csv.writer(hf), csv.writer(ff)
            history_columns = ['odb_file', 'step', 'domain', 'history_region', 'variable', 'sample_index',
                         'segment', 'step_time', 'odb_total_time', 'value']
            field_columns = ['odb_file', 'step', 'domain', 'frame_index', 'value_index', 'increment_number', 'step_time',
                         'odb_total_time', 'variable', 'position', 'instance', 'node_label', 'element_label',
                         'integration_point', 'section_point', 'face', 'component_labels_json', 'values_json']
            hw.writerow(history_columns)
            fw.writerow(field_columns)
            hj.write('['); fj.write('[')
            counts = [0, 0]
            def emit(writer, stream, columns, row, which):
                writer.writerow(row)
                data = dict(zip(columns, row))
                for key in ('component_labels_json', 'values_json'):
                    if key in data:
                        data[key[:-5]] = json.loads(data.pop(key))
                if counts[which]:
                    stream.write(',\n')
                stream.write(json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str, allow_nan=False))
                counts[which] += 1
            for step_name, step in odb.steps.items():
                domain = str(step.domain)
                start = number(safe_attr(step, 'totalTime')) if domain == 'TIME' else None
                frames = list(step.frames)
                si = {'name': step_name, 'procedure': str(safe_attr(step, 'procedure', '未知')),
                      'domain': domain, 'declared_time_period': number(safe_attr(step, 'timePeriod')),
                      'odb_step_start_total_time': start, 'frames': [], 'history_regions': {},
                      'field_variables': [], 'extracted': not step_filter or step_name in step_filter}
                for region_name, region in step.historyRegions.items():
                    si['history_regions'][region_name] = {}
                    for name, output_var in region.historyOutputs.items():
                        data = output_var.data
                        si['history_regions'][region_name][name] = {'count': len(data),
                            'description': str(safe_attr(output_var, 'description', ''))}
                        energy = name.upper() in ('ALLKE', 'ALLIE', 'ALLAE', 'ETOTAL')
                        if not si['extracted'] or (history_filter and region_name not in history_filter and not energy) or not matches(name, HISTORY_PREFIXES):
                            continue
                        segment, previous = 0, None
                        for index, pair in enumerate(data):
                            t, value = number(pair[0]), number(pair[1])
                            if t is not None and previous is not None and t < previous:
                                segment += 1
                            if t is not None:
                                previous = t
                            total = start + t if start is not None and t is not None else None
                            emit(hw, hj, history_columns, [meta['odb_path'], step_name, domain, region_name, name, index,
                                         segment, t, total, value], 0)
                all_fields = set()
                for frame_index, frame in enumerate(frames):
                    t = number(safe_attr(frame, 'frameValue'))
                    frame_vars = list(frame.fieldOutputs.keys())
                    all_fields.update(frame_vars)
                    si['frames'].append({'index': frame_index, 'frame_value': t,
                        'increment_number': safe_attr(frame, 'incrementNumber'), 'field_variables': frame_vars})
                    if not si['extracted']:
                        continue
                    for name, field in frame.fieldOutputs.items():
                        if not matches(name, FIELD_PREFIXES):
                            continue
                        labels = list(safe_attr(field, 'componentLabels', ()))
                        for value_index, val in enumerate(field.values):
                            inst = safe_attr(val, 'instance')
                            section = safe_attr(val, 'sectionPoint')
                            vals = components(val)
                            emit(fw, fj, field_columns, [meta['odb_path'], step_name, domain, frame_index, value_index,
                                safe_attr(frame, 'incrementNumber'), t,
                                start + t if start is not None and t is not None else None, name,
                                str(safe_attr(val, 'position', '')), safe_attr(inst, 'name', ''),
                                safe_attr(val, 'nodeLabel', ''), safe_attr(val, 'elementLabel', ''),
                                safe_attr(val, 'integrationPoint', ''), safe_attr(section, 'number', ''),
                                str(safe_attr(val, 'face', '')), json.dumps(labels), json.dumps(vals)], 1)
                si['field_variables'] = sorted(all_fields)
                si['last_saved_frame_value'] = si['frames'][-1]['frame_value'] if si['frames'] else None
                meta['steps'].append(si)
            hj.write(']\n'); fj.write(']\n')
        with open(os.path.join(output, 'odb_inventory.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, allow_nan=False)
        return meta
    finally:
        odb.close()


def main():
    parser = argparse.ArgumentParser(description='以 Abaqus Python 只读提取 ODB')
    parser.add_argument('--odb', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--step', action='append')
    parser.add_argument('--history-region', action='append')
    args = parser.parse_args()
    try:
        extract(args.odb, args.output, args.step, args.history_region)
    except Exception as exc:
        print('ODB 读取失败: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
