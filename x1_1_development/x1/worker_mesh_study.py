"""Abaqus/CAE noGUI 子进程：仅修改指定工作副本，绝不启动作业。"""
import json
import traceback
from abaqus import openMdb
from abaqusConstants import ANALYSIS_CHECKS, STABLE_TIME_INCREMENT, SIZE, DEVIATION_FACTOR, MIN_SIZE_FACTOR
from abaqusConstants import ELEM_SHAPE, TECHNIQUE, TET, FREE, NON_DEFAULT, OFF
import mesh
import interaction
import load


def metric(part, criterion):
    try:
        result=part.verifyMeshQuality(criterion=criterion)
        out={}
        for key in ('average','worst','numElements'):
            if key in result: out[key]=result[key]
        for key in ('failedElements','warningElements','naElements'):
            if key in result:
                out[{'failedElements':'errors','warningElements':'warnings','naElements':'unavailable'}[key]]=len(result[key])
        if 'worstElement' in result: out['worst_element']=result['worstElement'].label
        return out
    except Exception as exc:
        return {'error':str(exc)}


def region_inventory(model):
    """只记录名称和区域计数；相等不代表物理联系已验证。"""
    a=model.rootAssembly
    result={'constraints':sorted(model.constraints.keys()), 'interactions':sorted(model.interactions.keys()),
            'boundary_conditions':sorted(model.boundaryConditions.keys()), 'sets':{}, 'surfaces':{}}
    for kind in ('sets','surfaces'):
        repo=getattr(a,kind)
        for name in sorted(repo.keys()):
            region=repo[name]; item={}
            for attribute in ('faces','cells','nodes','elements','referencePoints','edges'):
                try: item[attribute]=len(getattr(region,attribute))
                except Exception: pass
            result[kind][name]=item
    return result


def snapshot(model,part):
    quality=metric(part,ANALYSIS_CHECKS)
    if 'error' in quality or any(key not in quality for key in ('errors','warnings','unavailable')):
        raise RuntimeError('Abaqus 网格分析检查失败或关键计数未知：%s' % quality)
    return {'nodes':len(part.nodes),'elements':len(part.elements),'cells':len(part.cells),'faces':len(part.faces),
            'vertices':len(part.vertices),'quality':quality,
            'stable_time':metric(part,STABLE_TIME_INCREMENT),'regions':region_inventory(model)}


def run():
    with open('request.json') as f: request=json.load(f)
    openMdb(pathName=request['model_copy'])
    from abaqus import mdb
    model=mdb.models[request['model']]; part=model.parts[request['part']]; trial=request['trial']
    before=snapshot(model,part)
    if trial['kind']!='baseline':
        if not len(part.cells): raise RuntimeError('部件没有可网格划分的实体区域。')
        for cell in part.cells:
            if part.getMeshControl(region=cell,attribute=ELEM_SHAPE)!=TET or part.getMeshControl(region=cell,attribute=TECHNIQUE)!=FREE:
                raise RuntimeError('目标包含非自由四面体区域；为保护原有单元与控制，已跳过。')
        size=part.getPartSeeds(attribute=SIZE)
        if trial['kind']=='size':
            if size<=0: raise RuntimeError('未设置整体布种尺寸，跳过比例试验。')
            dev=part.getPartSeeds(attribute=DEVIATION_FACTOR); minimum=part.getPartSeeds(attribute=MIN_SIZE_FACTOR)
        part.deleteMesh()
        if trial['kind']=='size': part.seedPart(size=size*trial['factor'],deviationFactor=dev,minSizeFactor=minimum)
        elif trial['kind']=='mapped_off': part.setMeshControls(regions=part.cells[:],allowMapped=OFF)
        elif trial['kind']=='alternate': part.setMeshControls(regions=part.cells[:],algorithm=NON_DEFAULT)
        else: raise RuntimeError('未知试验。')
        part.generateMesh()
        if not len(part.elements): raise RuntimeError('生成网格为空。')
        model.rootAssembly.regenerate()
    after=snapshot(model,part)
    if trial['kind']!='baseline': mdb.save()
    result={'ok':True,'before':before,'after':after,'region_inventory_equal':before['regions']==after['regions'],
            'geometry_counts_equal':all(before[k]==after[k] for k in ('cells','faces','vertices'))}
    with open('worker_result.json','w') as f: json.dump(result,f,indent=2)


try:
    run()
except Exception:
    with open('worker_result.json','w') as f: json.dump({'ok':False,'error':traceback.format_exc()},f,indent=2)
    raise
