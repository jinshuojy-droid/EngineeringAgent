"""Abaqus Python: 只读导出历程与指定帧的网格检查数据。"""
import argparse
import csv
import json
import os
import sys
from v3_geometry import assess_element


def values(v):
    try:
        data = v.dataDouble
    except Exception:
        data = v.data
    try:
        return [float(x) for x in data]
    except TypeError:
        return [float(data)]


def extract(path, out, scan, focus_instance=None, focus_element=None, peeq_global=False):
    from odbAccess import openOdb
    odb = openOdb(path=path, readOnly=True)
    meta = {'odb': path, 'steps': [], 'scan': scan, 'mesh': [], 'focus': [], 'peeq_focus': [], 'peeq_global': [], 'errors': []}
    try:
        with open(os.path.join(out, 'history.csv'), 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['odb', 'step', 'region', 'variable', 'sample', 'step_time', 'value'])
            for name, step in odb.steps.items():
                frames = step.frames
                info = {'name': name, 'domain': str(step.domain), 'procedure': str(getattr(step, 'procedure', '未知')),
                        'time_period': float(step.timePeriod), 'frame_count': len(frames),
                        'frames': [{'index': i, 'time': float(frame.frameValue), 'variables': list(frame.fieldOutputs.keys())}
                                   for i, frame in enumerate(frames)], 'history_regions': []}
                meta['steps'].append(info)
                for region_name, region in step.historyRegions.items():
                    info['history_regions'].append({'name': region_name, 'variables': list(region.historyOutputs.keys())})
                    for key, history in region.historyOutputs.items():
                        if key.upper() not in ('U1','U2','U3','V1','V2','V3','RF1','RF2','RF3','ALLAE','ALLIE','ALLKE','ALLWK','ALLFD','ALLVD','ALLIHE','ALLPW','ALLCW','ALLMW','ALLHF','ETOTAL','ALLSE','ALLPD','ALLCD','ALLDMD','ALLDC','ALLFC','CAREA') and not key.upper().startswith('CFNM'):
                            continue
                        points=history.data
                        if points is None:
                            meta['errors'].append('历程数据为空：%s / %s / %s'%(name,region_name,key))
                            continue
                        for index, point in enumerate(points):
                            w.writerow([path, name, region_name, key, index, point[0], point[1]])
                if peeq_global:
                    for fi,fr in enumerate(frames):
                        if 'PEEQ' not in fr.fieldOutputs:continue
                        field_values=fr.fieldOutputs['PEEQ'].values
                        if not field_values:continue
                        sample=max(field_values,key=lambda v:values(v)[0])
                        inst=getattr(sample,'instance',None)
                        meta['peeq_global'].append({'step':name,'frame':fi,'time':float(fr.frameValue),
                            'instance':inst.name if inst is not None else None,
                            'element':getattr(sample,'elementLabel',None),'value':values(sample)[0],
                            'integration_point':getattr(sample,'integrationPoint',None)})
                if focus_instance and focus_element:
                    try:
                        inst=odb.rootAssembly.instances[focus_instance]
                        elem=inst.getElementFromLabel(focus_element)
                        conn=list(elem.connectivity)
                        nodes=[inst.getNodeFromLabel(n) for n in conn]
                        initial=[list(n.coordinates) for n in nodes]
                        for fi,fr in enumerate(frames):
                            if 'PEEQ' in fr.fieldOutputs:
                                samples=[v for v in fr.fieldOutputs['PEEQ'].getSubset(region=inst).values if v.elementLabel == focus_element]
                                if samples:
                                    sample=max(samples,key=lambda v:values(v)[0])
                                    meta['peeq_focus'].append({'step':name,'frame':fi,'time':float(fr.frameValue),
                                        'instance':focus_instance,'element':focus_element,'value':values(sample)[0],
                                        'integration_point':getattr(sample,'integrationPoint',None)})
                            if 'U' not in fr.fieldOutputs: continue
                            u=[]
                            for node in nodes:
                                samples=fr.fieldOutputs['U'].getSubset(region=node).values
                                if not samples:break
                                u.append(values(samples[0]))
                            if len(u)!=8:continue
                            current=[[float(x)+float(v) for x,v in zip(xyz,dis)] for xyz,dis in zip(initial,u)]
                            assessed=assess_element(str(elem.type),conn,initial,current)
                            if assessed.get('deformed'):
                                meta['focus'].append({'step':name,'frame':fi,'time':float(fr.frameValue),
                                    'instance':focus_instance,'element':focus_element,
                                    'minimum_scaled_jacobian':assessed['deformed']['minimum_scaled_jacobian'],
                                    'coordinates':current,'connectivity':conn})
                    except (KeyError,ValueError,IndexError) as exc:
                        meta['errors'].append('关键单元追踪失败: '+str(exc))
                if not len(frames) or scan == 'none':
                    continue
                indices = range(len(frames)) if scan == 'full' else sorted(set([0, len(frames)-1]))
                for index in indices:
                    frame = frames[index]
                    result = {'step': name, 'frame': index, 'time': float(frame.frameValue),
                              'supported': 0, 'checked': 0, 'skipped': 0, 'unsupported': 0, 'worst': [], 'negative_count': 0}
                    meta['mesh'].append(result)
                    field = frame.fieldOutputs['U'] if 'U' in frame.fieldOutputs else None
                    displacement = {}
                    if field:
                        for value in field.values:
                            instance = getattr(value, 'instance', None)
                            if instance is not None and getattr(value, 'nodeLabel', None) is not None:
                                displacement[(instance.name, value.nodeLabel)] = values(value)
                    for instance_name, instance in odb.rootAssembly.instances.items():
                        coordinates = {n.label: list(n.coordinates) for n in instance.nodes}
                        for element in instance.elements:
                            if str(element.type).upper() not in ('C3D8','C3D8R'):
                                result['unsupported'] += 1
                                continue
                            result['supported'] += 1
                            conn = list(element.connectivity)
                            if any(n not in coordinates or (instance_name,n) not in displacement for n in conn):
                                result['skipped'] += 1
                                continue
                            initial = [coordinates[n] for n in conn]
                            current = [[float(x)+float(u) for x,u in zip(coordinates[n],displacement[(instance_name,n)])] for n in conn]
                            metrics = assess_element(str(element.type), conn, initial, current)
                            geom = metrics.get('deformed')
                            if not geom or geom['minimum_scaled_jacobian'] is None:
                                result['skipped'] += 1
                                continue
                            result['checked'] += 1
                            val = geom['minimum_scaled_jacobian']
                            if val <= 0: result['negative_count'] += 1
                            item = {'instance':instance_name,'element':element.label,'value':val,
                                    'initial_centroid':[sum(p[i] for p in initial)/8.0 for i in range(3)]}
                            result['worst'].append(item)
                            result['worst'].sort(key=lambda x:x['value'])
                            del result['worst'][10:]
        meta['complete'] = True
    finally:
        odb.close()
        with open(os.path.join(out, 'odb_summary.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--odb',required=True);p.add_argument('--output',required=True)
    p.add_argument('--scan',choices=('quick','full','none'),default='quick')
    p.add_argument('--focus-instance');p.add_argument('--focus-element',type=int)
    p.add_argument('--peeq-global',action='store_true')
    a=p.parse_args()
    extract(a.odb,a.output,a.scan,a.focus_instance,a.focus_element,a.peeq_global)


if __name__ == '__main__': main()
