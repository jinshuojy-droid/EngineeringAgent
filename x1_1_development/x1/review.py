"""X1.3 综合只读前处理核查。结果严格区分事实、默认行为和未验证项。"""
import ast
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import re

from .core import audit_sensor, digest, new_run
from .thermal import audit, read_keywords

THERMAL_ELEMENTS = {'C3D8T', 'C3D8RT', 'C3D4T', 'C3D10MT', 'C3D6T'}
DEFAULT_DOC = 'https://docs.software.vt.edu/abaqusv2026/English/SIMACAEITNRefMap/simaitn-c-thermalinteraction.htm'


def _ev(r):
    return {'file': r['file'], 'line': r['line']}


def _key(value):
    return str(value or '').strip().upper()


def _numbers(line):
    return [item.strip() for item in line.split(',') if item.strip()]


def model_regions(records):
    """重建直接定义的零件、实例与节点集；不递归展开复杂集合。"""
    part = None
    nodes = defaultdict(set)
    elements = defaultdict(list)
    sections = defaultdict(list)
    instances = {}
    nsets = defaultdict(lambda: defaultdict(set))
    unresolved = []
    for r in records:
        k, p = r['keyword'], r['params']
        if k == 'part': part = _key(p.get('name'))
        elif k == 'end part': part = None
        elif k == 'instance' and p.get('name') and p.get('part'):
            instances[_key(p['name'])] = {'part': _key(p['part']), 'evidence': _ev(r)}
        elif k == 'node' and part:
            for row in r['data']:
                vals = _numbers(row['text'])
                if vals:
                    try: nodes[part].add(int(vals[0]))
                    except ValueError: unresolved.append({'reason':'节点定义未解析', 'evidence':_ev(r)})
        elif k == 'element' and part:
            elements[part].append({'type': _key(p.get('type')), 'evidence': _ev(r)})
        elif k in ('solid section','shell section') and part:
            sections[part].append({'material':p.get('material'), 'elset':p.get('elset'), 'evidence':_ev(r)})
        elif k == 'nset':
            name, inst = _key(p.get('nset')), _key(p.get('instance'))
            if not name: continue
            if not inst:
                unresolved.append({'reason':'节点集缺少可直接识别的实例：'+name, 'evidence':_ev(r)})
                continue
            for row in r['data']:
                try:
                    values = [int(v) for v in _numbers(row['text'])]
                    if 'generate' in p:
                        if len(values) != 3 or values[2] <= 0 or values[1] < values[0] or (values[1]-values[0])//values[2] > 2000000:
                            raise ValueError('invalid node range')
                        values = range(values[0], values[1]+1, values[2])
                    nsets[name][inst].update(values)
                except ValueError:
                    unresolved.append({'reason':'节点集包含嵌套、非数字或无法解析的范围：'+name, 'evidence':_ev(r)})
    regions = []
    for name, instance in instances.items():
        part_name = instance['part']
        if part_name not in nodes and part_name not in elements: continue
        regions.append({'instance':name, 'part':part_name, 'node_count':len(nodes[part_name]),
                        'element_types':elements[part_name], 'sections':sections[part_name], 'evidence':instance['evidence']})
    initial=[]
    for r in records:
        if r['keyword'] != 'initial conditions' or _key(r['params'].get('type')) != 'TEMPERATURE': continue
        for row in r['data']:
            cols=_numbers(row['text'])
            if len(cols)<2:
                unresolved.append({'reason':'初始温度记录不能解析', 'evidence':{'file':r['file'],'line':row['line']}})
                continue
            try: value=float(cols[1])
            except ValueError:
                unresolved.append({'reason':'初始温度值不能解析', 'evidence':{'file':r['file'],'line':row['line']}});continue
            name=_key(cols[0]);coverage=[]
            for inst, selected in nsets.get(name,{}).items():
                expected=nodes.get(instances.get(inst,{}).get('part'),set())
                missing=expected-selected
                coverage.append({'instance':inst,'selected':len(selected),'total':len(expected),
                    'missing':len(missing) if expected else None,'extra':len(selected-expected) if expected else None,
                    'scope':'直接节点集' if expected else '实例或节点定义未识别'})
            initial.append({'set':name,'temperature':value,'evidence':{'file':r['file'],'line':row['line']},'coverage':coverage})
    coverage_summary=[]
    for instance in regions:
        name=instance['instance']; expected=nodes[instance['part']]; covered=set(); seen={};conflicts=set()
        for entry in initial:
            for label in nsets.get(entry['set'],{}).get(name,set()):
                if label in seen and seen[label]!=entry['temperature']:conflicts.add(label)
                seen[label]=entry['temperature'];covered.add(label)
        coverage_summary.append({'instance':name,'total':len(expected),'covered':len(expected & covered),
                                 'missing':len(expected-covered) if expected else None,'conflicts':len(conflicts),
                                 'evidence':[x['evidence'] for x in initial]})
    return {'instances':regions, 'initial_temperature':initial, 'coverage_summary':coverage_summary, 'unresolved':unresolved,
            'scope':'仅覆盖直接数字节点集与部件定义；嵌套、组合及非均匀初值须单独核对。'}


def contact_properties(records, steps):
    """分属性归集显式生热设置与引用；默认值只作为有条件的文档提示。"""
    props={}; current=None; assignments=[]
    for r in records:
        k,p=r['keyword'],r['params']
        if k == 'surface interaction':
            current=_key(p.get('name'))
            props[current]={'name':p.get('name'), 'evidence':_ev(r), 'gap_heat_generation':None,
                            'conductance':None, 'user': 'user' in p}
        elif k in ('material','amplitude','boundary','step','end assembly'):
            current=None
        elif current and k in ('gap heat generation','gap conductance'):
            props[current]['gap_heat_generation' if k=='gap heat generation' else 'conductance']={
                'evidence':_ev(r),'data':[x['text'] for x in r['data']]}
        if k == 'contact pair':
            assignments.append({'kind':'contact pair','property':p.get('interaction'),
                                'surfaces':[x['text'] for x in r['data']],'step':r['step'],'evidence':_ev(r)})
        elif k == 'contact property assignment':
            for row in r['data']:
                parts=[x.strip() for x in row['text'].split(',')]
                assignments.append({'kind':'general contact assignment','property':parts[-1] if parts else '',
                                    'surfaces':parts[:-1],'step':r['step'],'evidence':{'file':r['file'],'line':row['line']}})
    for prop in props.values():
        if prop['gap_heat_generation']:
            prop['heat_status']='显式设置'
        elif prop['user']:
            prop['heat_status']='用户接触行为，不能套用内置默认值'
        else:
            prop['heat_status']='未显式设置；适用内置热接触时文档默认生热比例 1.0、分配 0.5，需结合实际接触验证'
        prop['conductance_status']='显式设置' if prop['conductance'] else '未显式设置；内置闭合接触通常采用默认导热，需验证适用性'
    return {'properties':list(props.values()),'assignments':assignments,'default_source':DEFAULT_DOC,
            'scope':'仅记录输入中的直接引用；不证明接触实际发生、传热量或完整属性覆盖。'}


def output_evidence(records, steps):
    """记录每个分析步实际声明的输出，不凭名称推断跨步传播。"""
    result=[]; current=None
    for r in records:
        if r['keyword']=='output':
            current={'step':r['step'],'kind':'field' if 'field' in r['params'] else 'history' if 'history' in r['params'] else 'other',
                     'params':r['params'],'evidence':_ev(r),'variables':[]}
            result.append(current)
        elif r['keyword'] in ('node output','element output','contact output') and current:
            for row in r['data']:
                current['variables'] += [{'name':_key(v),'kind':r['keyword'],'file':r['file'],'line':row['line']} for v in _numbers(row['text'])]
        elif r['keyword'] not in ('node output','element output','contact output'):
            current=None
    temp=[]
    for step in steps:
        matches=[v for block in result if block['step']==step['name'] and block['kind']=='field'
                 for v in block['variables'] if v['name'] in ('NT','NT11')]
        temp.append({'step':step['name'],'status':'已显式请求' if matches else '未找到显式 NT 输出；默认或传播需核实', 'evidence':matches})
    return {'temperature_by_step':temp,'blocks':result}


def material_review(audit, provenance):
    """材料记录只映射用户提供的数据；未记录来源显示未知。"""
    materials=[]; warnings=[]
    records=provenance.get('materials',{}) if provenance else {}
    if not isinstance(records,dict): raise ValueError('材料来源文件的 materials 必须为对象')
    for material in audit['materials']:
        name=material['name'];props=material['properties'];record=records.get(name,{})
        if not isinstance(record,dict): raise ValueError('材料来源记录必须为对象：'+str(name))
        items=[]
        for key in ('density','conductivity','specific heat','inelastic heat fraction','elastic','plastic','rate dependent','expansion'):
            for prop in props[key]:
                rows=prop['data']; first=rows[0]['text'] if rows else ''
                meta=record.get(key,{})
                if not isinstance(meta,dict):raise ValueError('材料属性记录必须为对象：'+str(name)+'/'+key)
                try: actual=float(_numbers(first)[0])
                except (IndexError,ValueError):actual=None
                if actual is not None and not math.isfinite(actual):actual=None
                item={'property':key,'raw':first,'value':actual,'evidence':{'file':prop['file'],'line':prop['line']},
                      'provenance':meta or {'status':'未记录','source':None,'unit':None}}
                if 'value' in meta:
                    try: expected=float(meta['value'])
                    except (ValueError,TypeError):raise ValueError('材料记录数值无效：'+str(name)+'/'+key)
                    if not math.isfinite(expected):raise ValueError('材料记录数值必须有限：'+str(name)+'/'+key)
                    item['record_matches']=actual is not None and math.isclose(actual,expected,rel_tol=1e-9,abs_tol=1e-12)
                    if not item['record_matches']: warnings.append({'material':name,'property':key,'message':'材料记录数值与输入不一致','evidence':item['evidence']})
                if key=='plastic' and _key(prop['params'].get('hardening'))=='JOHNSON COOK':
                    try:
                        vals=[float(x) for x in _numbers(first)]
                        if len(vals)>=6:
                            item['johnson_cook']={'A':vals[0],'B':vals[1],'n':vals[2],'m':vals[3],
                                                  'melting':vals[4],'transition':vals[5]}
                            parameter_meta=meta.get('parameters',{})
                            if not isinstance(parameter_meta,dict):raise ValueError('材料参数记录必须为对象：'+str(name))
                            item['parameter_matches']={}
                            for param, expected in parameter_meta.items():
                                if param not in item['johnson_cook']:raise ValueError('不支持的 Johnson–Cook 参数：'+str(param))
                                try: expected=float(expected)
                                except (ValueError,TypeError):raise ValueError('Johnson–Cook 来源参数无效：'+str(param))
                                if not math.isfinite(expected):raise ValueError('Johnson–Cook 来源参数必须有限：'+str(param))
                                matched=math.isclose(item['johnson_cook'][param],expected,rel_tol=1e-9,abs_tol=1e-12)
                                item['parameter_matches'][param]=matched
                                if not matched:warnings.append({'material':name,'property':key,'message':'来源记录的 '+param+' 与输入不一致','evidence':item['evidence']})
                            if vals[3]==0:warnings.append({'material':name,'property':key,'message':'Johnson–Cook 热软化指数 m 为零，温度软化未启用','evidence':item['evidence']})
                            elif vals[4]<=vals[5]:warnings.append({'material':name,'property':key,'message':'熔化温度不高于转变温度，请核对','evidence':item['evidence']})
                    except ValueError:warnings.append({'material':name,'property':key,'message':'Johnson–Cook 参数无法解析','evidence':item['evidence']})
                items.append(item)
        materials.append({'name':name,'evidence':{'file':material['file'],'line':material['line']},'properties':items})
    return {'materials':materials,'warnings':warnings,'provenance_file':provenance.get('_file') if provenance else None,
            'scope':'仅记录显式材料数据和用户提供的来源；不自动判断牌号、温度依赖或物理适用性。'}


def log_review(source):
    """日志按同目录同名关联，时间顺序只作线索，不证明输入一致。"""
    logs=[]
    for suffix in ('.com','.log','.sta','.dat'):
        path=source.with_suffix(suffix)
        if not path.is_file():continue
        before=path.stat()
        text=path.read_bytes().decode('latin-1');lines=text.splitlines();items=[]
        for i,line in enumerate(lines,1):
            up=line.upper()
            if re.search(r'\*\*\*\s*(WARNING|ERROR)',up) or 'COMPLETED SUCCESSFULLY' in up or ('ABAQUS JOB' in up and ('COMPLETED' in up or 'ABORT' in up)):
                items.append({'line':i,'text':' '.join(s.strip() for s in lines[i-1:min(i+3,len(lines))])[:450]})
        after=path.stat()
        obj={'path':str(path),'sha256':digest(path),'mtime':datetime.fromtimestamp(after.st_mtime).isoformat(), 'items':items,
             'changed_during_read':(before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns)}
        if suffix=='.com':
            chk=re.search(r"['\"]datacheck['\"]\s*:\s*(1|0|ON|OFF|True|False)\b",text,re.I)
            app=re.search(r"['\"]applicationName['\"]\s*:\s*['\"]([^'\"]+)['\"]",text,re.I)
            obj['application_name']=app.group(1) if app else None
            obj['datacheck']=(chk.group(1).lower() in ('1','on','true')) if chk else (
                True if app and app.group(1).lower()=='datacheck' else False if app and app.group(1).lower()=='analysis' else None)
            obj['datacheck_basis']='datacheck 参数' if chk else 'applicationName 记录' if app else '未识别'
            match=re.search(r"['\"]user['\"]\s*:\s*([\"\'][^\r\n]*?[\"\'])\s*,",text)
            if match:
                try:obj['user_subroutine']=ast.literal_eval(match.group(1))
                except (SyntaxError,ValueError):obj['user_subroutine']='无法解析'
        logs.append(obj)
    lock=source.with_suffix('.lck')
    return {'files':logs,'lock_file':str(lock) if lock.is_file() else None,
            'association':'仅按同目录同名关联；时间或名称不足以证明与当前 INP 内容一致，需作业证据或哈希核对。'}


def subroutine_review(records, steps, sensors, logs):
    """比对最近记录中的用户子程序名、用户幅值和直写的传感器名。"""
    amplitude=[{'name':r['params'].get('name'), 'evidence':_ev(r)} for r in records
               if r['keyword']=='amplitude' and _key(r['params'].get('definition'))=='USER']
    job=next((x for x in logs['files'] if x['path'].lower().endswith('.com')),None)
    path=Path(job['user_subroutine']) if job and job.get('user_subroutine') not in (None,'无法解析','') else None
    result={'amplitudes':amplitude,'job_record':job['path'] if job else None,
            'subroutine':str(path) if path else None,'sensors':[],'amplitude_comparisons':[],
            'readable':False,'status':'未读取子程序；需要核对当前作业配置',
            'scope':'仅识别源代码中直接写出的名称；动态名称、条件分支和实际运行行为需单独验证。'}
    if not path or not path.is_file():return result
    source=path.read_text(encoding='latin-1');result['sha256']=digest(path)
    result['readable']=True
    result['status']='已读取同名作业记录指向的源文件；仍需确认属于当前输入及当前作业'
    calls=list(re.finditer(r"VGETSENSORVALUE\s*\(\s*['\"]([^'\"]+)['\"]",source,re.I))
    for call in calls:
        name=call.group(1);refs=[]
        for step in steps:
            hits=[s for s in sensors if s['step']==step['name'] and s['registered_sensor'] and _key(s['sensor_name'])==_key(name)]
            refs.append({'step':step['name'],'matches':len(hits),'lines':[s['line'] for s in hits]})
        result['sensors'].append({'name':name,'line':source.count('\n',0,call.start())+1,'steps':refs})
    cases={_key(m.group(1)) for m in re.finditer(r"AMPNAME\s*\.EQ\.\s*['\"]([^'\"]+)['\"]",source,re.I)}
    for item in amplitude:
        result['amplitude_comparisons'].append({'name':item['name'],'match_in_source':_key(item['name']) in cases,'evidence':item['evidence']})
    return result


def diagnose(basic, model, materials, output, records, units):
    """仅针对可证实的输入事实给出待核查事项和动作。"""
    findings=list(basic['findings'])
    coupled=any(s['type']=='coupled' for s in basic['steps'])
    if not coupled:
        return findings
    for instance in model['instances']:
        unsupported=[e for e in instance['element_types'] if e['type'] not in THERMAL_ELEMENTS]
        if unsupported:
            findings.append({'status':'unknown','summary':instance['instance']+' 使用未识别的温度—位移单元类型；不等于无温度自由度。',
                             'evidence':unsupported[0]['evidence'],'action':'查证该单元库及目标区域的温度自由度。'})
        if not instance['element_types']:
            findings.append({'status':'unknown','summary':instance['instance']+' 没有识别到零件单元。',
                             'evidence':instance['evidence'],'action':'核对网格、实例和包含文件。'})
    for info in model['coverage_summary']:
        if info['missing'] and model['initial_temperature']:
            findings.append({'status':'unknown','summary':'初始温度定义未证明覆盖 %s 全部节点。'%info['instance'],
                             'evidence':info['evidence'][0] if info['evidence'] else None,
                             'action':'核对其他初始温度定义和节点集，不要直接认定温度缺失。'})
        if info['conflicts']:
            findings.append({'status':'unknown','summary':'%s 有 %d 个节点出现在不同温度初值定义中。'%(info['instance'],info['conflicts']),
                             'evidence':info['evidence'][0] if info['evidence'] else None,
                             'action':'核对节点集重叠及输入次序，不自动判断最终值。'})
    if not model['initial_temperature']:
        findings.append({'status':'unknown','summary':'未识别到显式初始温度。','evidence':None,'action':'核对初始条件及 INCLUDE，确认单位与区域。'})
    assigned={_key(s['material']) for inst in model['instances'] for s in inst['sections'] if s.get('material')}
    for material in basic['materials']:
        if _key(material['name']) not in assigned:continue
        missing=[p for p in ('density','specific heat','conductivity') if not material['properties'][p]]
        if missing:
            findings.append({'status':'unknown','summary':'已指派材料 %s 未识别到 %s。'%(material['name'],', '.join(missing)),
                             'evidence':{'file':material['file'],'line':material['line']},
                             'action':'结合单元类型、材料定义及 INCLUDE 核对所需热参数。'})
    for w in materials['warnings']:
        findings.append({'status':'warning','summary':str(w['material'])+'：'+w['message'],
                         'evidence':w['evidence'],'action':'核对材料牌号、参数、温度单位及来源，必要时重新标定。'})
    for item in output['temperature_by_step']:
        if not item['evidence']:
            findings.append({'status':'unknown','summary':str(item['step'])+' 未找到显式节点温度输出请求。',
                             'evidence':None,'action':'检查输出默认值和跨步传播，按需请求 NT。'})
    constants=[r for r in records if r['keyword']=='physical constants']
    if units=='N-mm-s-tonne-C' and not any('absolute zero' in r['params'] for r in constants):
        findings.append({'status':'warning','summary':'用户指定摄氏度，但未找到显式绝对零度。',
                         'evidence':None,'action':'核对模型物理常数；摄氏度下绝对零度通常为 -273.15。'})
    if units in ('N-mm-s-tonne-C','N-mm-s-tonne-K'):
        expected=-273.15 if units.endswith('-C') else 0.0
        for r in constants:
            try: value=float(r['params']['absolute zero'])
            except (KeyError,ValueError,TypeError):continue
            if not math.isclose(value,expected,abs_tol=1e-6):
                findings.append({'status':'warning','summary':'显式绝对零度与用户选择的温标不一致。',
                                 'evidence':_ev(r),'action':'确认温度单位和物理常数，统一后重新写入输入文件。'})
    return findings


def make_review(path, provenance_path=None, unit_system=None):
    source=Path(path).resolve()
    if not source.is_file() or source.suffix.lower()!='.inp':raise ValueError('需要存在的 INP 文件')
    provenance=None
    if provenance_path:
        provpath=Path(provenance_path).resolve()
        provenance=json.loads(provpath.read_text(encoding='utf-8-sig'))
        if not isinstance(provenance,dict):raise ValueError('材料来源文件应为 JSON 对象')
        provenance['_file']=str(provpath)
    initial_hash=digest(source)
    basic=audit(source);records,files,warnings=read_keywords(source)
    model=model_regions(records);contact=contact_properties(records,basic['steps'])
    outputs=output_evidence(records,basic['steps']);materials=material_review(basic,provenance)
    sensors=audit_sensor(source)['history_outputs'];logs=log_review(source)
    subroutine=subroutine_review(records,basic['steps'],sensors,logs)
    after_files=[];changed=[]
    for record in files:
        path=Path(record['path'])
        after=digest(path) if path.is_file() else None
        after_files.append({'path':record['path'],'sha256':after})
        if after!=record['sha256']:changed.append(record['path'])
    findings=diagnose(basic,model,materials,outputs,records,unit_system)
    if warnings:
        findings.append({'status':'unknown','summary':'输入文件或 INCLUDE 未完整读取。','evidence':{'file':str(source),'line':1},
                         'action':'核对包含文件路径、读取权限和递归引用后重新检查。'})
    if len(files)>1:
        findings.append({'status':'unknown','summary':'传感器登记检查只扫描主输入文件，未覆盖 INCLUDE。',
                         'evidence':{'file':str(source),'line':1},
                         'action':'逐一核对包含文件中的历程输出和传感器名称，不能据此断定未注册。'})
    if not model['instances']:
        findings.append({'status':'unknown','summary':'未识别到可检查的零件实例和节点。','evidence':{'file':str(source),'line':1},
                         'action':'核对模型结构、孤立网格和包含文件，不能推断初始温度覆盖。'})
    if not unit_system:
        findings.append({'status':'unknown','summary':'模型单位制尚未由用户确认。','evidence':None,
                         'action':'确认长度、力、时间、质量和温标后再审核材料单位。'})
    trial=[(m,p) for m in materials['materials'] for p in m['properties']
           if p['provenance'].get('status') in ('试算','待核实','未记录')]
    if trial:
        m,p=trial[0]
        findings.append({'status':'unknown','summary':'部分材料参数为试算或未记录来源，尚不能证明适用实际材料。',
                         'evidence':p['evidence'],'action':'按实际牌号和状态核对材料资料，更新参数来源记录。'})
    if logs['files']:
        findings.append({'status':'unknown','summary':'同名日志与当前输入文件尚未建立内容级关联。',
                         'evidence':{'file':logs['files'][0]['path'],'line':1},
                         'action':'核对作业生成时间和输入哈希；不要仅按名称认定同次求解。'})
    else:
        findings.append({'status':'unknown','summary':'输入目录没有找到同名作业日志。','evidence':None,
                         'action':'由用户决定是否进行数据检查，再读取对应日志。'})
    if subroutine['amplitudes'] and not subroutine['readable']:
        findings.append({'status':'unknown','summary':'输入含用户幅值，但未能读取同名作业记录中的子程序文件。',
                         'evidence':subroutine['amplitudes'][0]['evidence'],'action':'在当前 CAE 作业设置中核对子程序文件。'})
    for item in subroutine['sensors']:
        missing=[x['step'] for x in item['steps'] if x['matches']!=1]
        if missing:
            findings.append({'status':'unknown','summary':'源码传感器 %s 在以下分析步的同名注册数不是 1：%s。'%(item['name'],', '.join(missing)),
                             'evidence':{'file':subroutine['subroutine'],'line':item['line']},
                             'action':'核对子程序调用分支、传感器作用域和数据检查日志；名称比对不代表运行已验证。'})
    for item in subroutine['amplitude_comparisons']:
        if not item['match_in_source']:
            findings.append({'status':'unknown','summary':'未在子程序中识别用户幅值 %s 的同名分支。'%item['name'],
                             'evidence':item['evidence'],'action':'检查子程序名称匹配方式、源文件版本及当前作业配置。'})
    constants=[{'absolute_zero':r['params'].get('absolute zero'),'evidence':_ev(r)} for r in records if r['keyword']=='physical constants']
    out={'version':'X1.3','input':str(source),'job':source.stem,'source_sha256_before':initial_hash,
         'source_sha256_after':digest(source),'files':files,'input_read_warnings':warnings,
         'files_after':after_files,'files_changed':changed,
         'unit_system':unit_system or '未确认','physical_constants':constants,'steps':basic['steps'],'regions':model,'materials':materials,'section_controls':basic['sections'],
         'contact':contact,'outputs':outputs,'sensors':sensors,'subroutine':subroutine,'logs':logs,'findings':findings,
         'limitations':['仅解析部分 INP 语义；嵌套集合、复杂约束、默认值和传播需要进一步核对。',
                        '传感器登记检查仅扫描主 INP，不扫描 INCLUDE 中的传感器定义。',
                        '同名日志与用户子程序路径仅是来源线索，不能证明属于当前输入版本。',
                        '材料资料与单位由用户提供或确认；不可由数值推断牌号或物理可信度。',
                        '不启动求解或数据检查，不能仅凭输入确认接触、温升、焊接或结果可靠。']}
    if initial_hash!=out['source_sha256_after'] or changed:
        out['input_read_warnings'].append('读取期间输入或 INCLUDE 文件内容发生变化。')
        out['findings'].append({'status':'unknown','summary':'输入读取期间内容发生变化，报告可能不是一致快照。',
                                'evidence':{'file':str(source),'line':1},'action':'等待输入稳定后重新检查。'})
    return out


def render(result):
    """中文摘要保留证据、默认值边界和下一步。"""
    attention=[x for x in result['findings'] if x['status'] in ('unknown','warning','error')]
    log_warnings=sum(1 for f in result['logs']['files'] for x in f['items'] if re.search(r'\*\*\*\s*WARNING',x['text'],re.I))
    lines=['# X1.3 热模型前处理检查','','作业：'+result['job'],'输入：'+result['input'],
           '输入 SHA-256：'+result['source_sha256_before'],
           '输入及 INCLUDE 读取前后哈希：'+('一致' if result['source_sha256_before']==result['source_sha256_after'] and not result['files_changed'] else '不一致'),
           '单位制：'+result['unit_system']+'；由用户指定，未指定时不推断。','',
           '## 核心结论','- 本次识别 %d 个分析步、%d 个可见实例；存在 %d 个程序提示或证据不足项。'%(len(result['steps']),len(result['regions']['instances']),len(attention)),
           '- 同名日志中提取到 %d 条警告记录（跨文件可能重复）；日志与当前输入的关联仍待核对。'%log_warnings,
           '- 解析到的设置不等于物理验证；日志按同名关联且尚未证明为当前输入版本。', '', '## 分析步']
    names={'coupled':'热力耦合','adiabatic':'绝热','mechanical':'力学','unknown':'未识别'}
    for s in result['steps']:lines.append('- %s：%s（%s），时间周期 %s；%s:%s。'%(s['name'],names.get(s['type'],s['type']),s['keyword'],s.get('time_period','未知'),s['file'],s['line']))
    if not result['steps']:lines.append('- 未识别到分析步；请核对输入文件。')
    lines+=['','## 单元与初始温度']
    for r in result['regions']['instances']:
        types=', '.join(x['type'] for x in r['element_types']) or '未知'
        lines.append('- %s / %s：节点 %s，单元类型 %s；%s:%s。'%(r['instance'],r['part'],r['node_count'],types,r['evidence']['file'],r['evidence']['line']))
    for init in result['regions']['initial_temperature']:
        lines.append('- 初始温度 %s，节点集 %s；%s:%s。'%(init['temperature'],init['set'],init['evidence']['file'],init['evidence']['line']))
        for c in init['coverage']:
            lines.append('  - %s：已选 %s / %s 节点；未覆盖 %s。'%(c['instance'],c['selected'],c['total'],c['missing']))
    if not result['regions']['initial_temperature']:lines.append('- 未识别到显式初始温度；请核对初值及模型单位。')
    for item in result['physical_constants']:
        lines.append('- 物理常数绝对零度：%s；%s:%s。'%(item['absolute_zero'],item['evidence']['file'],item['evidence']['line']))
    relevant_sets={i['set'] for i in result['regions']['initial_temperature']}
    for u in result['regions']['unresolved']:
        if any(name in u['reason'] for name in relevant_sets):lines.append('- 未解析：%s；%s:%s。'%(u['reason'],u['evidence']['file'],u['evidence']['line']))
    lines+=['','## 截面、单元与沙漏控制']
    for section in result.get('section_controls', []):
        types=', '.join('%s × %s'%(v['type'],v['count']) for v in section['element_types']) or '未解析'
        lines.append('- %s / %s：%s；控制 %s；沙漏 %s；映射 %s；%s:%s。'%(
            section['part'] or '未知',section['elset'] or '未知',types,section['controls'] or '未显式指定',
            section['hourglass'],'完整' if section['complete'] else '需核查',section['file'],section['line']))
        if section.get('controls_evidence'):
            ev=section['controls_evidence']; lines.append('  控制定义：%s:%s。'%(ev['file'],ev['line']))
        if section.get('hourglass_stiffness'):
            ev=section['hourglass_stiffness']; lines.append('  HOURGLASS STIFFNESS：%s:%s。'%(ev['file'],ev['line']))
    lines+=['','## 材料与参数来源']
    prop_names={'density':'密度','conductivity':'导热系数','specific heat':'比热','inelastic heat fraction':'非弹性热份额',
                'elastic':'弹性','plastic':'塑性','rate dependent':'应变率相关','expansion':'热膨胀'}
    for m in result['materials']['materials']:
        lines.append('- %s：'%m['name'])
        for p in m['properties']:
            prov=p['provenance'];jc=p.get('johnson_cook')
            lines.append('  - %s = %s；单位 %s；来源状态 %s，资料 %s；输入 %s:%s。'%(prop_names.get(p['property'],p['property']),p['raw'],prov.get('unit') or '未确认',prov.get('status','未记录'),prov.get('source') or '未记录',p['evidence']['file'],p['evidence']['line']))
            if prov.get('note'):lines.append('    说明：'+str(prov['note']))
            if jc:lines.append('    Johnson–Cook：m=%s，熔点=%s，转变温度=%s。'%(jc['m'],jc['melting'],jc['transition']))
            if p.get('record_matches') is False:lines.append('    **来源记录数值与输入不一致，需核对。**')
            if any(not v for v in p.get('parameter_matches',{}).values()):lines.append('    **Johnson–Cook 来源参数与输入不一致，需核对。**')
    for w in result['materials']['warnings']:lines.append('- 待核查：%s / %s：%s；%s:%s。'%(w['material'],w['property'],w['message'],w['evidence']['file'],w['evidence']['line']))
    lines+=['','## 接触属性']
    for c in result['contact']['assignments']:
        kind={'contact pair':'接触对','general contact assignment':'通用接触属性指派'}.get(c['kind'],c['kind'])
        surface='、'.join(s for s in c['surfaces'] if s) or '默认指派区域，需核对包含清单'
        lines.append('- %s：%s，属性 %s，分析步 %s；%s:%s。'%(kind,surface,c['property'],c['step'],c['evidence']['file'],c['evidence']['line']))
    for p in result['contact']['properties']:
        lines.append('- %s：%s；界面导热：%s；%s:%s。'%(p['name'],p['heat_status'],p['conductance_status'],p['evidence']['file'],p['evidence']['line']))
        if p['gap_heat_generation']:
            e=p['gap_heat_generation'];lines.append('  生热输入：%s；%s:%s。'%(', '.join(e['data']),e['evidence']['file'],e['evidence']['line']))
    lines+=['','## 温度输出与传感器']
    for t in result['outputs']['temperature_by_step']:
        lines.append('- %s：%s。'%(t['step'],t['status']))
        for e in t['evidence']:lines.append('  来源：%s:%s。'%(e['file'],e['line']))
    for s in result['sensors']:
        if s['registered_sensor']:
            lines.append('- %s / %s：%s / %s；%s:%s。'%(s['step'],s['sensor_name'],s['kind'],s['region'],result['input'],s['line']))
    sub=result['subroutine']
    lines.append('- 子程序核对：%s；文件 %s。'%(sub['status'],sub['subroutine'] or '未知'))
    for item in sub['sensors']:
        lines.append('  - 源代码传感器 %s，%s:%s；各步注册数 %s。'%(item['name'],sub['subroutine'],item['line'],
                     ', '.join(str(x['step'])+'='+str(x['matches']) for x in item['steps'])))
    for item in sub['amplitude_comparisons']:
        lines.append('  - 用户幅值 %s：%s；%s:%s。'%(item['name'],'源码发现同名分支' if item['match_in_source'] else '未识别同名分支，需人工核对',item['evidence']['file'],item['evidence']['line']))
    lines+=['','## 日志与作业关联','- '+result['logs']['association']]
    for log in result['logs']['files']:
        check={True:'是',False:'否',None:'未识别'}.get(log.get('datacheck'),'未识别')
        details=('；记录为数据检查='+check+'；依据='+str(log.get('datacheck_basis','未识别'))+'；用户子程序='+str(log.get('user_subroutine','未识别'))) if log['path'].lower().endswith('.com') else ''
        lines.append('- %s，修改时间 %s%s。'%(log['path'],log['mtime'],details))
        if log['changed_during_read']:lines.append('  - 日志读取过程中发生变化，本次快照不完整。')
        for item in log['items'][:12]:
            raw=item['text'];up=raw.upper()
            if 'ELEMENTS ARE DISTORTED' in up:
                count=re.search(r'(\d+)\s+ELEMENTS ARE DISTORTED',up)
                msg='初始网格存在%s个畸变单元警告；定位 WarnElemDistorted，按单元实际作用核对。'%(count.group(1) if count else '未识别数量的')
            elif 'SECONDARY NODES INVOLVED IN *TIE' in up:msg='Tie 部分次表面节点未调整；核对节点、层间位置和约束，不能直接认定失效。'
            elif 'CDISP IS NOT AVAILABLE' in up:msg='部分接触对不支持 CDISP 输出；按具体接触类型核对所需变量。'
            elif 'ABSOLUTE ZERO TEMPERATURE' in up:msg='绝对零度未显式给出；确认温标及模型物理常数。'
            elif 'CONTACT PAIR IN ABAQUS/EXPLICIT WILL BE DEPRECATED' in up:msg='显式接触对未来可能被移除；先核对接触实现和影响，再考虑是否改用通用接触。'
            elif 'NUMBER OF ARGUMENTS FOR USER SUBROUTINES' in up:msg='用户子程序接口版本提醒；对照当前接口和编译记录核对，不单凭提醒断定错误。'
            elif 'PARAMETERS TIME, SMOOTH' in up and 'DEFINITION=USER' in up:msg='用户幅值定义中的 TIME 等参数被忽略；核对 VUAMP 实际使用的时间变量。'
            elif 'EXTREMELY LARGE NUMBER OF INCREMENTS' in up:msg='求解增量数可能超过 200 万；核对临界单元和稳定增量，不能据此估计准确耗时。'
            elif 'BOUNDARY,TYPE=DISPLACEMENT' in up:msg='位移边界跨步跳变提示；检查两步交界的位移及幅值。'
            elif 'THERE ARE' in up and 'WARNING MESSAGES IN THE DATA' in up:msg='DAT 文件包含前处理警告；具体内容见 DAT 记录。'
            elif 'COMPLETED' in up:msg='日志记录完成；仍须确认是否为数据检查和是否对应当前输入。'
            else:msg=raw
            lines.append('  - %s:%s：%s'%(log['path'],item['line'],msg))
    lines+=['','## 后续核查']
    if result['logs']['lock_file']:lines.append('- 检测到锁文件 %s；锁文件本身不证明作业正在运行，请核对状态。'%result['logs']['lock_file'])
    for f in result['findings']:
        level={'unknown':'待核实','warning':'警告','error':'错误','info':'信息'}.get(f['status'],f['status'])
        lines.append('- %s：%s %s'%(level,f['summary'],f.get('action','')))
        e=f.get('evidence')
        if isinstance(e,dict) and e.get('file'):lines.append('  来源：%s:%s。'%(e['file'],e.get('line','未知')))
    if result['unit_system']=='未确认':lines.append('- 确认模型单位制后，再判断温度、物性和绝对零度。')
    lines+=['- 核对实际材料牌号、热参数来源以及温度依赖；不要把试算值当作已标定数据。',
            '- 核对初始温度覆盖、界面默认导热及接触、传感器与子程序在实际作业中的对应关系。',
            '- 是否执行数据检查或提交作业，由用户操作；检查完成后核对新日志与本次输入的一致性。',
            '', '## 范围与限制']
    lines += ['- '+x for x in result['limitations']]
    if result['input_read_warnings']:lines += ['- 输入读取警告：'+x for x in result['input_read_warnings']]
    return '\n'.join(lines)+'\n'


def export_review(result, output_dir=None):
    """新建独立检查目录，不覆盖输入与既有报告。"""
    source=Path(result['input']).resolve()
    base=Path(output_dir).resolve() if output_dir else Path(__file__).resolve().parents[1]/'runs'
    if base==source.parent or source.parent in base.parents:
        raise ValueError('报告目录不能位于原始输入目录')
    out=new_run(base,'thermal_review')
    (out/'结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    (out/'报告.md').write_text(render(result),encoding='utf-8')
    return out
