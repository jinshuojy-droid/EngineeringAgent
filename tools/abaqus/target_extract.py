"""Abaqus Python 只读提取指定单元及其节点，不导出全模型。"""
import argparse
import csv
import json
import math
import os
import sys


def num(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def value_data(value):
    try:
        data = value.dataDouble
    except Exception:
        data = value.data
    try:
        return [num(x) for x in data]
    except TypeError:
        return [num(data)]


def stat(path):
    st = os.stat(path)
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def inside(path, root):
    try:
        return os.path.normcase(os.path.commonpath([path,root])) == os.path.normcase(root)
    except ValueError:
        return False


def extract(odb_path, instance_name, element_label, output):
    from odbAccess import openOdb
    root = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))
    output = os.path.realpath(output)
    odb_path = os.path.realpath(odb_path)
    if not inside(output, root) or output == root:
        raise ValueError('输出必须位于项目内')
    if inside(output,os.path.dirname(odb_path)) or inside(output,os.path.join(root,'versions')):
        raise ValueError('输出不能位于源 ODB 目录或版本存档目录')
    if os.path.exists(output):
        raise ValueError('输出目录已存在')
    before = stat(odb_path)
    odb = openOdb(path=odb_path, readOnly=True)
    try:
        instance = odb.rootAssembly.instances[instance_name]
        element = instance.getElementFromLabel(element_label)
        target_set = set(element.connectivity)
        neighborhood = [e for e in instance.elements if len(target_set.intersection(e.connectivity)) >= 4]
        node_labels = sorted({label for e in neighborhood for label in e.connectivity})
        nodes = [instance.getNodeFromLabel(n) for n in node_labels]
        target_nodes = [instance.getNodeFromLabel(n) for n in element.connectivity]
        os.makedirs(output)
        meta = {'odb_path': odb_path, 'read_only': True, 'instance': instance_name,
                'element': element_label, 'element_type': str(element.type),
                'connectivity': list(element.connectivity), 'nodes': [], 'frames': [],
                'neighborhood': [{'label': e.label, 'type': str(e.type), 'connectivity': list(e.connectivity)} for e in neighborhood],
                'field_index_note': 'field.csv value_index 为该帧局部提取序号；以实例、标签、位置、积分点追溯 ODB。',
                'history': [], 'warnings': [], 'stat_before': before}
        warning_keys = set()
        for n in nodes:
            meta['nodes'].append({'label': n.label, 'coordinates': [num(x) for x in n.coordinates]})
        with open(os.path.join(output, 'field.csv'), 'w', encoding='utf-8', newline='') as f:
            cols = ['odb_file', 'step', 'domain', 'frame_index', 'value_index', 'increment_number',
                    'step_time', 'odb_total_time', 'variable', 'position', 'instance', 'node_label',
                    'element_label', 'integration_point', 'section_point', 'face', 'component_labels_json', 'values_json']
            w = csv.writer(f); w.writerow(cols)
            for step_name, step in odb.steps.items():
                domain = str(step.domain); start = num(getattr(step, 'totalTime', None)) if domain == 'TIME' else None
                for frame_index, frame in enumerate(step.frames):
                    time = num(frame.frameValue); index = 0
                    meta['frames'].append({'step': step_name, 'domain': domain, 'frame_index': frame_index,
                                           'step_time': time, 'increment_number': frame.incrementNumber})
                    for name, field in frame.fieldOutputs.items():
                        is_u = name == 'U'; is_peeq = name == 'PEEQ'
                        is_contact = name.upper().startswith(('CPRESS', 'COPEN', 'CSLIP', 'CDISP'))
                        if not (is_u or is_peeq or is_contact):
                            continue
                        regions = [element] if is_peeq else (target_nodes if is_contact else nodes)
                        for region in regions:
                            try:
                                values = field.getSubset(region=region).values
                            except Exception as exc:
                                key=(step_name,frame_index,name,type(exc).__name__)
                                if key not in warning_keys:
                                    warning_keys.add(key)
                                    meta['warnings'].append({'step':step_name,'frame_index':frame_index,
                                        'variable':name,'message':str(exc)})
                                continue
                            for value in values:
                                inst = getattr(value, 'instance', None)
                                section = getattr(value, 'sectionPoint', None)
                                w.writerow([odb_path, step_name, domain, frame_index, index, frame.incrementNumber,
                                            time, start + time if start is not None and time is not None else '',
                                            name, str(getattr(value, 'position', '')), getattr(inst, 'name', ''),
                                            getattr(value, 'nodeLabel', ''), getattr(value, 'elementLabel', ''),
                                            getattr(value, 'integrationPoint', ''), getattr(section, 'number', ''),
                                            str(getattr(value, 'face', '')), json.dumps(list(field.componentLabels)),
                                            json.dumps(value_data(value), allow_nan=False)])
                                index += 1
                for region_name, region in step.historyRegions.items():
                    for name, history in region.historyOutputs.items():
                        if name.upper() not in ('ALLAE', 'ALLIE', 'ALLKE', 'ETOTAL', 'ALLWK', 'ALLSE'):
                            continue
                        for sample_index, pair in enumerate(history.data):
                            meta['history'].append({'step': step_name, 'domain': domain, 'region': region_name,
                                'variable': name, 'sample_index': sample_index, 'step_time': num(pair[0]), 'value': num(pair[1])})
        with open(os.path.join(output, 'entities.csv'), 'w', encoding='utf-8', newline='') as f:
            w=csv.writer(f); w.writerow(['odb_file','instance','entity_type','label','coordinates_json','element_type','connectivity_json'])
            for n in meta['nodes']:
                w.writerow([odb_path, instance_name, 'node', n['label'], json.dumps(n['coordinates']), '', ''])
            for e in neighborhood:
                w.writerow([odb_path, instance_name, 'element', e.label, '', str(e.type), json.dumps(list(e.connectivity))])
        with open(os.path.join(output, 'energy_history.csv'), 'w', encoding='utf-8', newline='') as f:
            w=csv.writer(f);w.writerow(['odb_file','step','domain','history_region','variable','sample_index','segment','step_time','odb_total_time','value'])
            state = {}
            for row in meta['history']:
                key=(row['step'],row['region'],row['variable']); previous, segment=state.get(key,(None,0)); time=row['step_time']
                if time is not None and previous is not None and time < previous: segment += 1
                state[key]=(time if time is not None else previous,segment)
                w.writerow([odb_path,row['step'],row['domain'],row['region'],row['variable'],row['sample_index'],segment,time,'',row['value']])
        meta['stat_after'] = stat(odb_path)
        with open(os.path.join(output, 'target_manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, allow_nan=False)
    finally:
        odb.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--odb', required=True); p.add_argument('--instance', required=True)
    p.add_argument('--element', type=int, required=True); p.add_argument('--output', required=True)
    args=p.parse_args()
    try:
        extract(args.odb,args.instance,args.element,args.output)
    except Exception as e:
        print('局部提取失败：%s: %s' % (type(e).__name__,e),file=sys.stderr); return 2
    return 0


if __name__ == '__main__': sys.exit(main())
