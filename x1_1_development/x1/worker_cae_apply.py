"""仅在独立 CAE 工作副本改动内部节点；官方检查通过后保存副本。"""
import json
import traceback
from abaqus import openMdb
from abaqusConstants import ANALYSIS_CHECKS
import mesh
import meshEdit
import interaction
import load

def checks(part):
    result=part.verifyMeshQuality(criterion=ANALYSIS_CHECKS)
    names=('warningElements','failedElements','naElements')
    if any(name not in result for name in names): raise RuntimeError('官方网格检查返回字段不完整。')
    return {name:sorted(e.label for e in result[name]) for name in names}

def volume_violations(nodes, guards):
    """按 Abaqus 实际节点坐标复算有向体积。"""
    failed = {}
    for guard in guards:
        label, connectivity, previous, floor = guard[:4]
        baseline = guard[4] if len(guard) > 4 else previous
        a, b, c, d = (nodes[n].coordinates for n in connectivity)
        ux, uy, uz = (b[i] - a[i] for i in range(3))
        vx, vy, vz = (c[i] - a[i] for i in range(3))
        wx, wy, wz = (d[i] - a[i] for i in range(3))
        volume = (ux*(vy*wz-vz*wy) - uy*(vx*wz-vz*wx) + uz*(vx*wy-vy*wx)) / 6
        if volume * previous <= 0 or abs(volume) < floor:
            failed[label] = {'label': label, 'nodes': connectivity, 'previous_signed_volume': previous,
                             'baseline_signed_volume': baseline, 'candidate_signed_volume': volume,
                             'ratio': abs(volume / baseline) if baseline else None,
                             'minimum_accepted_volume': floor}
    return failed

def run(history):
    with open('request.json') as stream: request=json.load(stream)
    openMdb(pathName=request['model_copy'])
    from abaqus import mdb
    model=mdb.models[request['model']]; part=model.parts[request['part']]
    initial=checks(part)
    if initial!=request['baseline_checks']: raise RuntimeError('工作副本官方检查与导出基准不一致。')
    lookup={n.label:(i,n) for i,n in enumerate(part.nodes)}; edits=request['edits']; selected=[]
    bylabel={item['label']:item for item in edits}
    for item in edits:
        index,node=lookup[item['label']]
        if max(abs(a-b) for a,b in zip(node.coordinates,item['before']))>1e-12:
            raise RuntimeError('节点原始坐标与导出基准不一致。')
        selected.append((index,node,item['after']))
    selected.sort(key=lambda v:v[0])
    if selected:
        part.editNode(nodes=tuple(v[1] for v in selected),coordinates=tuple(tuple(v[2]) for v in selected))
    active=set(bylabel)
    guards = request['volume_checks']
    for iteration in range(20):
        model.rootAssembly.regenerate()
        result=checks(part)
        new_bad=(set(result['warningElements'])-set(initial['warningElements'])) | set(result['failedElements']) | set(result['naElements'])
        volume_bad = volume_violations({node.label: node for node in part.nodes}, guards)
        step={'iteration':iteration,'warnings':len(result['warningElements']),'new_bad_labels':sorted(new_bad),
              'volume_violation_labels':sorted(volume_bad),'volume_failures':list(volume_bad.values()),'active':len(active)}
        history.append(step)
        rejected = new_bad | set(volume_bad)
        if not rejected: break
        e_lookup={e.label:e for e in part.elements}
        affected=set()
        for label in rejected:
            if label not in e_lookup: raise RuntimeError('新增异常单元不存在，停止。')
            affected.update(n.label for n in e_lookup[label].getNodes())
        rollback=sorted(active & affected,key=lambda label:lookup[label][0])
        if not rollback: raise RuntimeError('新增异常未关联已移动节点，停止。')
        step['reverted_nodes']=rollback
        part.editNode(nodes=tuple(lookup[label][1] for label in rollback),
                      coordinates=tuple(tuple(bylabel[label]['before']) for label in rollback))
        active.difference_update(rollback)
    else:
        raise RuntimeError('官方检查多次回退后仍有新增问题。')
    if len(result['warningElements'])>=len(initial['warningElements']):
        raise RuntimeError('官方警告未减少；不保存候选。')
    mdb.save()
    with open('worker_result.json','w') as stream:
        json.dump({'ok':True,'completed':True,'before':initial,'after':result,'moved_nodes':len(active),'history':history},stream,indent=2)

history=[]
try: run(history)
except Exception:
    with open('worker_result.json','w') as stream:
        json.dump({'ok':False,'error':traceback.format_exc(),'history':history},stream,indent=2)
    raise
