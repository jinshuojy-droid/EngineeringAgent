"""在 CAE 副本中导出原生网格和 Abaqus 官方分析检查，不提交作业。"""
import json
import traceback
from abaqus import openMdb
from abaqusConstants import ANALYSIS_CHECKS, STABLE_TIME_INCREMENT
import mesh
import interaction
import load

def run():
    with open('request.json') as stream: request=json.load(stream)
    openMdb(pathName=request['model_copy'])
    from abaqus import mdb
    model=mdb.models[request['model']]; part=model.parts[request['part']]
    if not len(part.elements): raise RuntimeError('目标部件没有网格。')
    check=part.verifyMeshQuality(criterion=ANALYSIS_CHECKS)
    if 'warningElements' not in check or 'failedElements' not in check or 'naElements' not in check:
        raise RuntimeError('官方网格检查返回字段不完整，停止。')
    try:
        stable=part.verifyMeshQuality(criterion=STABLE_TIME_INCREMENT)
        worst=stable.get('worstElement')
        stable_info={'worst':stable.get('worst'), 'average':stable.get('average'),
                     'worst_element':worst.label if worst is not None else None}
    except Exception as exc:
        stable_info={'error':str(exc)}
    result={'ok':True,'completed':True,'stable_time':stable_info,'nodes':[[n.label]+list(n.coordinates) for n in part.nodes],
        'elements':[[e.label,str(e.type)]+[n.label for n in e.getNodes()] for e in part.elements],
        'warnings':[e.label for e in check['warningElements']],
        'errors':[e.label for e in check['failedElements']],
        'unavailable':[e.label for e in check['naElements']],
        'instances':[name for name,inst in model.rootAssembly.instances.items() if inst.partName==request['part']]}
    with open('worker_result.json','w') as stream: json.dump(result,stream,indent=2)

if __name__ == '__main__':
    try: run()
    except Exception:
        with open('worker_result.json','w') as stream: json.dump({'ok':False,'error':traceback.format_exc()},stream,indent=2)
        raise
