"""X1.3 热前处理：单位换算、INP 证据索引和受控副本修改。"""
import difflib
import json
import math
from pathlib import Path
import re
from .core import ROOT, digest, new_run
from .sections import section_audit

DOC_COUPLED='https://docs.software.vt.edu/abaqusv2026/English/SIMACAEANLRefMap/simaanl-c-couptempdisp.htm'
DOC_ADIABATIC='https://docs.software.vt.edu/abaqusv2026/English/SIMACAEANLRefMap/simaanl-c-adiabaticanal.htm'


def convert(quantity, value, unit):
    """明确单位的单向换算，不推定材料属性和模型单位制。"""
    value=float(value)
    if not math.isfinite(value) or value <= 0: raise ValueError('输入值必须为有限正数。')
    factors={('specific_heat','J/(kg*K)'):(1e6,'N*mm/(tonne*K)'),
             ('conductivity','W/(m*K)'):(1.0,'N/(s*K)'),
             ('density','kg/m3'):(1e-12,'tonne/mm3')}
    if (quantity,unit) not in factors: raise ValueError('不支持的物理量或原单位；请参照使用说明。')
    factor,target=factors[quantity,unit]
    return {'quantity':quantity,'input_value':value,'input_unit':unit,'output_value':value*factor,
            'output_unit':target,'unit_system':'N-mm-s-tonne-K','factor':factor,
            'note':'仅做量纲换算；不能确认数值是否适用于实际材料。'}


def read_keywords(path):
    """保留关键词行号、分析步和材料；递归读取 INCLUDE。"""
    records=[]; warnings=[]; files=[]; state={'step':None,'material':None}
    def read(p, stack):
        p=Path(p).resolve()
        if p in stack:
            warnings.append('INCLUDE 循环：'+str(p));return
        if len(stack)>=32:
            warnings.append('INCLUDE 层级超过限制：'+str(p));return
        try: raw=p.read_bytes()
        except OSError as e: warnings.append('无法读取 %s：%s'%(p,e));return
        files.append({'path':str(p),'sha256':digest(p)})
        text=raw.decode('latin-1'); current=None
        for no,original in enumerate(text.splitlines(),1):
            line=original.strip()
            if not line or line.startswith('**'):continue
            if not line.startswith('*'):
                if current is not None:current['data'].append({'line':no,'text':line})
                continue
            fields=[v.strip() for v in line[1:].split(',')];key=fields[0].lower();params={}
            for token in fields[1:]:
                k,sep,v=token.partition('=')
                if k.strip():params[k.strip().lower()]=v.strip().strip('"\'') if sep else True
            if key=='include':
                include=params.get('input')
                if isinstance(include,str):read(p.parent/include,stack+(p,))
                else:warnings.append('INCLUDE 缺少路径：%s:%s'%(p,no))
                current=None;continue
            if key=='step':state['step']=params.get('name')
            if key=='end step':state['step']=None
            if key=='material':state['material']=params.get('name')
            if key in ('part','assembly','step','end step'):state['material']=None
            current={'keyword':key,'params':params,'step':state['step'],'material':state['material'],
                     'file':str(p),'line':no,'data':[]};records.append(current)
    read(path,())
    return records,files,warnings


def audit(path):
    """只读报告实际输入，不把未发现和物理错误画等号。"""
    p=Path(path).resolve()
    if not p.is_file():raise ValueError('未找到 INP：'+str(p))
    records,files,warnings=read_keywords(p)
    groups={}
    for r in records:groups.setdefault(r['keyword'],[]).append(r)
    steps=[]
    for r in records:
        if r['keyword'] in ('dynamic','dynamic temperature-displacement','coupled temperature-displacement','static') and r['step']:
            mode=('coupled' if r['keyword'] in ('coupled temperature-displacement','dynamic temperature-displacement') else
                  'adiabatic' if 'adiabatic' in r['params'] else 'mechanical')
            period=None
            if r['data'] and r['keyword'] in ('dynamic','dynamic temperature-displacement'):
                cols=r['data'][0]['text'].split(',')
                if len(cols)>1:
                    try:period=float(cols[1])
                    except ValueError:pass
            steps.append({'name':r['step'],'type':mode,'keyword':r['keyword'],'file':r['file'],'line':r['line'],'time_period':period})
    recognized={s['name'] for s in steps}
    for r in groups.get('step',[]):
        name=r['params'].get('name')
        if name not in recognized:
            steps.append({'name':name,'type':'unknown','keyword':None,'file':r['file'],'line':r['line']})
    step_order={r['params'].get('name'):i for i,r in enumerate(groups.get('step',[]))}
    steps.sort(key=lambda s:step_order.get(s['name'],len(step_order)))
    materials=[]
    for m in groups.get('material',[]):
        name=m['params'].get('name'); entries=[r for r in records if r['material']==name]
        props={key:[{'file':r['file'],'line':r['line'],'data':r['data'],'params':r['params']}
                    for r in entries if r['keyword']==key] for key in
                    ('density','elastic','plastic','rate dependent','specific heat','conductivity','expansion','inelastic heat fraction')}
        materials.append({'name':name,'file':m['file'],'line':m['line'],'properties':props})
    elements=[{'type':r['params'].get('type'),'file':r['file'],'line':r['line']} for r in groups.get('element',[])]
    thermal_types={'C3D8T','C3D8RT','C3D4T','C3D10MT','C3D6T'}
    coupled=[x for x in elements if str(x['type']).upper() in thermal_types]
    relevant=['initial conditions','temperature','gap conductance','gap heat generation','rigid body','output','node output','element output','contact output']
    evidence={k:[{'file':r['file'],'line':r['line'],'step':r['step'],'params':r['params'],'data':r['data']}
                 for r in groups.get(k,[])] for k in relevant}
    findings=[]
    for step in steps:
        if step['type']=='unknown':findings.append({'status':'unknown','summary':str(step['name'])+' 的分析类型未识别。','evidence':step,'action':'检查该分析步原始关键词和求解类型。'})
        if step['type']=='mechanical':findings.append({'status':'info','summary':step['name']+' 为普通力学步，未显式启用绝热或耦合分析。','evidence':step,'action':'按研究目的选择分析类型。'})
        if step['type']=='coupled' and not coupled:findings.append({'status':'unknown','summary':step['name']+' 未识别到已支持的热—位移单元类型。','evidence':step,'action':'检查各零件单元及温度自由度；其他类型可能未收录。'})
    for m in materials:
        q=m['properties']
        if q['plastic'] and any(s['type']=='adiabatic' for s in steps):
            absent=[k for k in ('density','specific heat','inelastic heat fraction') if not q[k]]
            if absent:findings.append({'status':'warning','summary':m['name']+' 有塑性定义，未找到：'+', '.join(absent),'evidence':{'file':m['file'],'line':m['line']},'action':'核对该材料是否参与绝热生热，并补齐相关物性。'})
    recognized_all=bool(steps) and all(s['type']!='unknown' for s in steps)
    return {'input':str(p),'files':files,'complete':not warnings and recognized_all,'read_complete':not warnings,
            'analysis_recognition_complete':recognized_all,'warnings':warnings,'steps':steps,'materials':materials,
            'element_types':elements,'recognized_coupled_element_types':coupled,'sections':section_audit(records),'thermal_evidence':evidence,'findings':findings,
            'limitations':['截面只解析支持的零件级实体单元及集合；不支持的映射标记为未知，输出传播需另行核查。','未建立的界面热属性不能仅凭关键字不存在判断，应结合 Abaqus 默认值与具体接触方式核查。'],
            'sources':[DOC_COUPLED,DOC_ADIABATIC]}


def material_copy(path,output_dir,material,specific_heat,inelastic_fraction):
    """仅在新 INP 副本中为指定材料追加明确给定的两项热属性。"""
    source=Path(path).resolve();before=digest(source);raw=source.read_bytes();text=raw.decode('latin-1')
    if re.search(r'(?im)^\s*\*include\s*,',text):raise ValueError('包含 INCLUDE，请先处理输入依赖。')
    heat=float(specific_heat);fraction=float(inelastic_fraction)
    if not math.isfinite(heat) or heat<=0 or not math.isfinite(fraction) or not 0<=fraction<=1:raise ValueError('比热须为有限正数，生热分数须在 0 到 1。')
    lines=text.splitlines(keepends=True); matches=[]
    for i,line in enumerate(lines):
        match=re.match(r'^\s*\*material\s*,\s*name\s*=\s*([^,\r\n]+)',line,re.I)
        if match and match.group(1).strip().casefold()==material.casefold():matches.append(i)
    if len(matches)!=1:raise ValueError('未找到唯一目标材料。')
    start=matches[0];end=len(lines)
    for i in range(start+1,len(lines)):
        if re.match(r'^\s*\*(?:material|step|end\s+part|assembly|boundary|amplitude|surface\s+interaction)\b',lines[i],re.I):end=i;break
    existing=''.join(lines[start:end])
    if re.search(r'(?im)^\s*\*(specific heat|inelastic heat fraction)\b',existing):raise ValueError('目标材料已有对应属性，请先人工核对。')
    base=Path(output_dir).expanduser().resolve() if output_dir else ROOT/'runs'
    if base==source.parent or source.parent in base.parents:raise ValueError('输出目录不能位于原始作业目录。')
    insertion='*Specific Heat\n%.12g,\n*Inelastic Heat Fraction\n%.12g,\n'%(heat,fraction)
    lines.insert(start+1,insertion);edited=''.join(lines);folder=new_run(base,'thermal_material')
    target=folder/source.name;target.write_bytes(edited.encode('latin-1'))
    (folder/'修改对照.diff').write_text(''.join(difflib.unified_diff(text.splitlines(keepends=True),edited.splitlines(keepends=True),fromfile=str(source),tofile=str(target))),encoding='utf-8')
    result={'input':str(source),'output':str(target),'material':material,'specific_heat':heat,'inelastic_heat_fraction':fraction,
            'source_sha256_before':before,'source_sha256_after':digest(source),'output_sha256':digest(target),
            'note':'数值和单位由调用方提供，尚需核实；仅修改 INP 副本，未启用分析步、未更改 CAE、未运行数据检查。'}
    (folder/'结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'报告.md').write_text('# 热材料副本修改记录\n\n输入：`%s`\n\n输出：`%s`\n\n材料：%s；比热：%.12g；非弹性生热分数：%.12g。\n\n原文件哈希前后%s。数值和单位需核实；未修改 CAE，未启用绝热步骤，未进行数据检查。\n'%(source,target,material,heat,fraction,'一致' if before==result['source_sha256_after'] else '不一致'),encoding='utf-8')
    return result
