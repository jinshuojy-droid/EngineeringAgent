"""V3.5 结果侧辅助：配置候选、能量记录和只读进度。"""
import csv
import hashlib
import json
from pathlib import Path

from v31_support import estimate
from v3_inp import parse_inp


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def input_signature(inp):
    """根 INP 和所有解析到的 INCLUDE 必须完整且内容匹配。"""
    parsed=parse_inp(inp)
    if not parsed['complete']:raise ValueError('INP 或 INCLUDE 不完整，无法登记或自动匹配配置')
    files=[{'path':str(Path(f['path']).resolve()).casefold(),'sha256':f['sha256']} for f in parsed['files']]
    payload=json.dumps(files,sort_keys=True,ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def matching_config(registry_path, inp):
    """只在已确认且 INP 内容哈希一致时使用配置。"""
    if not inp or not Path(registry_path).is_file():return None
    registry=json.loads(Path(registry_path).read_text(encoding='utf-8'))
    try:key=input_signature(inp)
    except ValueError:return None
    match=registry.get('items',{}).get(key)
    if not match:return None
    config=Path(match['config'])
    if not config.is_file() or file_hash(config)!=match['config_sha256']:return None
    return {'path':str(config), 'input_signature':key, 'origin':'用户登记的 INP 和 INCLUDE 内容签名'}


def register_config(registry_path, inp, config):
    inp,config,registry_path=Path(inp).resolve(),Path(config).resolve(),Path(registry_path)
    if not isinstance(json.loads(config.read_text(encoding='utf-8')),dict):raise ValueError('检查配置必须是 JSON 对象')
    data=json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else {'schema_version':1,'items':{}}
    key=input_signature(inp)
    entry={'config':str(config),'config_sha256':file_hash(config),'input':str(inp)}
    previous=data['items'].get(key)
    if previous and previous != entry:raise ValueError('该 INP 已登记不同配置，不自动覆盖')
    data['items'][key]=entry
    registry_path.parent.mkdir(parents=True,exist_ok=True)
    registry_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return entry


def config_draft(model, summary):
    """只列候选，不将推测出的区域写入实际评估配置。"""
    draft = {'status': '待人工确认', 'automatic_application': False,
             'history_regions': [], 'contact_surfaces': [], 'rigid_reference_sets': [],
             'section_controls': [], 'stop_evidence': []}
    for step in (summary or {}).get('steps', []):
        for region in step.get('history_regions', []):
            variables = region.get('variables', [])
            interesting = [v for v in variables if v.upper() in ('U1','U2','U3','CAREA') or v.upper().startswith('CFNM')]
            if interesting:
                draft['history_regions'].append({'step':step['name'], 'region':region['name'],
                    'variables':interesting, 'source':'ODB 历程区域', 'confirmed':False})
    for entry in (model or {}).get('contacts', []):
        for line in entry.get('data', []):
            draft['contact_surfaces'].append({'text':line['text'], 'source':line['evidence'], 'confirmed':False})
    for entry in (model or {}).get('constraints', []):
        if entry.get('keyword') == 'rigid body':
            draft['rigid_reference_sets'].append({'parameters':entry['parameters'], 'source':entry['evidence'], 'confirmed':False})
    for entry in (model or {}).get('section_controls', []):
        draft['section_controls'].append({'parameters':entry['parameters'], 'source':entry['evidence']})
    for entry in (model or {}).get('outputs', []):
        if entry.get('parameters',{}).get('sensor'):
            draft['stop_evidence'].append({'keyword':entry['keyword'],'parameters':entry['parameters'],
                                           'source':entry['evidence'],'confirmed':False})
    return draft


def energy_balance(path, threshold=None, scale=None):
    """仅对每个分析步独立的全模型 ETOTAL 采样计算漂移。"""
    if not path or not Path(path).is_file():
        return {'status':'unknown','reason':'无历程 CSV'}
    groups = {}
    with Path(path).open(encoding='utf-8', newline='') as f:
        for line, row in enumerate(csv.DictReader(f), 2):
            if row.get('variable','').upper() != 'ETOTAL' or 'ASSEMBLY' not in row.get('region','').upper():
                continue
            try: point = {'time':float(row['step_time']), 'value':float(row['value']), 'line':line}
            except (ValueError, TypeError): continue
            groups.setdefault((row['step'],row['region']), []).append(point)
    if not groups:
        return {'status':'unknown','reason':'没有全模型 ETOTAL 记录','source':str(path)}
    records=[]
    for (step,region), values in groups.items():
        values.sort(key=lambda x:x['time'])
        start=values[0]; worst=max(values,key=lambda x:abs(x['value']-start['value']))
        records.append({'step':step,'region':region,'initial':start,'last':values[-1],
                        'max_abs_drift':abs(worst['value']-start['value']), 'worst':worst,
                        'samples':len(values), 'source':str(path)})
    result={'status':'observed','steps':records,
            'reason':'仅报告步内绝对漂移；相对判据需指定有物理意义的能量尺度。'}
    if threshold is not None and scale is not None:
        threshold,scale=float(threshold),float(scale)
        if threshold < 0 or scale <= 0:raise ValueError('能量漂移阈值应非负，能量尺度应为正')
        for row in records:row['normalized_drift']=row['max_abs_drift']/scale
        result.update({'status':'fail' if any(r['normalized_drift']>threshold for r in records) else 'observed',
                       'threshold':threshold,'scale':scale,
                       'reason':'使用用户指定的能量尺度筛查；未检查的分析步不视为通过。'})
    return result


def contact_area_history(path):
    """仅汇总 ODB 中实际提供的 CAREA，保留区域和来源行。"""
    if not path or not Path(path).is_file(): return []
    groups={}
    with Path(path).open(encoding='utf-8',newline='') as f:
        for line,row in enumerate(csv.DictReader(f),2):
            if row.get('variable','').upper() != 'CAREA': continue
            try: point={'time':float(row['step_time']),'value':float(row['value']),'line':line}
            except (ValueError,TypeError): continue
            groups.setdefault((row['step'],row['region']),[]).append(point)
    return [{'step':k[0], 'region':k[1], 'last':v[-1], 'max':max(v,key=lambda p:p['value']),
             'samples':len(v), 'source':str(path)} for k,v in groups.items()]


def watch(sta_path, step, target):
    """作业只读快照；不依据锁文件猜测运行或暂停状态。"""
    data=estimate(Path(sta_path),step,target)
    if data.get('status') == 'estimate':
        data['progress_fraction'] = min(1.0, data['current_time']/target) if target > 0 else None
    return data


def mesh_compare(review_files, step, region, variable, target, tolerance):
    """比较已有审查数据的同类历程；不替用户认定工况一致。"""
    cases=[]
    for filename in review_files:
        file=Path(filename).resolve();review=json.loads(file.read_text(encoding='utf-8'))
        history=file.parent/'history.csv';samples=[]
        with history.open(encoding='utf-8',newline='') as f:
            for line,row in enumerate(csv.DictReader(f),2):
                if (row.get('step'),row.get('region'),row.get('variable')) != (step,region,variable):continue
                try:samples.append({'time':float(row['step_time']),'value':float(row['value']),'line':line})
                except (ValueError,TypeError):continue
        chosen=min(samples,key=lambda r:abs(r['time']-target)) if samples else None
        if chosen and abs(chosen['time']-target)>tolerance:chosen=None
        mesh=(review.get('odb_summary') or {}).get('mesh',[])
        cases.append({'review':str(file),'odb':review.get('odb'),'job':review.get('job'),
                      'history':str(history),'sample':chosen,
                      'mesh_sample':mesh[-1] if mesh else None})
    return {'step':step,'region':region,'variable':variable,'target_time':target,'tolerance':tolerance,
            'cases':cases,'interpolation':'无；使用最近已保存样本',
            'same_conditions':'未自动确认；须核对几何、材料、载荷、约束和停止条件',
            'meaning':'样本差异不自动证明网格收敛。'}


def export_mesh_compare(result, output):
    """把对照记录保存为 CSV 和图，图中横坐标仅为输入工况顺序。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    output=Path(output);csv_path=output.with_suffix('.csv');png_path=output.with_suffix('.png')
    if csv_path.exists() or png_path.exists():raise ValueError('对照图或 CSV 已存在，不覆盖')
    with csv_path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f);writer.writerow(['job','odb','step','region','variable','requested_time','sample_time','value','source_file','source_line'])
        for case in result['cases']:
            sample=case.get('sample') or {}
            writer.writerow([case['job'],case['odb'],result['step'],result['region'],result['variable'],
                             result['target_time'],sample.get('time'),sample.get('value'),case['history'],sample.get('line')])
    valid=[(i,c) for i,c in enumerate(result['cases']) if c.get('sample')]
    if valid:
        fig,ax=plt.subplots();ax.plot([i+1 for i,c in valid],[c['sample']['value'] for i,c in valid],marker='o')
        ax.set_xlabel('Case order (not mesh size)');ax.set_ylabel(result['variable']);ax.set_title(result['step']+' saved samples');fig.tight_layout()
        fig.savefig(png_path,dpi=150);plt.close(fig)
    return {'csv':str(csv_path),'plot':str(png_path) if valid else None}
