"""在 Abaqus/CAE Python 中操作工作副本；不运行求解器。"""
import json
import sys
import traceback

from abaqus import openMdb
from abaqusConstants import ANALYSIS_CHECKS
import interaction
import load
import mesh


def snapshot(model):
    """记录零件网格与模型约束的数量，用于前后核对。"""
    out = {'parts': {}, 'constraints': {}, 'interactions': [], 'boundary_conditions': []}
    for name, part in model.parts.items():
        entry = {'cells': len(part.cells), 'nodes': len(part.nodes), 'elements': len(part.elements)}
        if len(part.elements):
            quality = part.verifyMeshQuality(criterion=ANALYSIS_CHECKS)
            entry['quality'] = {k: len(quality.get(k, [])) for k in
                                ('failedElements', 'warningElements', 'naElements')}
        out['parts'][name] = entry
    for name, obj in model.constraints.items():
        entry = {'type': obj.__class__.__name__, 'suppressed': bool(obj.suppressed)}
        for attr in ('bodyRegion', 'refPointRegion', 'main', 'secondary'):
            try:
                region = getattr(obj, attr)
                entry[attr] = {k: len(getattr(region, k)) for k in
                               ('cells', 'elements', 'faces', 'nodes', 'referencePoints')}
            except (AttributeError, TypeError):
                pass
        out['constraints'][name] = entry
    out['interactions'] = sorted(model.interactions.keys())
    out['boundary_conditions'] = sorted(model.boundaryConditions.keys())
    return out


def run():
    with open('request.json', 'r') as f:
        request = json.load(f)
    openMdb(pathName=request['model_copy'])
    from abaqus import mdb
    model = mdb.models[request['model']]
    before = snapshot(model)
    after = before
    if request['action'] == 'remesh':
        part = model.parts[request['part']]
        part.deleteMesh()
        part.seedPart(size=float(request['size']), deviationFactor=0.1, minSizeFactor=0.1)
        part.generateMesh()
        if not len(part.elements):
            raise RuntimeError('网格生成后没有单元，未保存修复版本。')
        model.rootAssembly.regenerate()
        after = snapshot(model)
        mdb.save()
    with open('worker_result.json', 'w') as f:
        json.dump({'ok': True, 'before': before, 'after': after}, f, ensure_ascii=True, indent=2)


try:
    run()
except Exception:
    with open('worker_result.json', 'w') as f:
        json.dump({'ok': False, 'error': traceback.format_exc()}, f, indent=2)
    raise
