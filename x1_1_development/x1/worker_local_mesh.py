"""Abaqus 内核中的选区检查、按实体区域试划和分区影响检查。"""
import json
import traceback

from abaqus import openMdb
from abaqusConstants import (ANALYSIS_CHECKS, STABLE_TIME_INCREMENT, XYPLANE,
                            YZPLANE, XZPLANE, DEFAULT, NON_DEFAULT, SINGLE, FINER)
import interaction
import load
import mesh
import step


def save_result(result):
    with open('worker_result.json', 'w') as stream:
        json.dump(result, stream, indent=2)


def entity_key(entity):
    owner = getattr(entity, 'instanceName', None)
    return [owner, getattr(entity, 'label', getattr(entity, 'index', None))]


def region_snapshot(region):
    shapes = {}
    for attr in ('vertices', 'edges', 'faces', 'cells', 'referencePoints'):
        items = getattr(region, attr, ())
        if hasattr(items, 'values'):
            items = items.values()
        if items:
            shapes[attr] = sorted((entity_key(item) for item in items), key=repr)
    if shapes:
        faces = getattr(region, 'faces', ())
        face_data = sorted(([entity_key(face), face.getSize(printResults=False), reference_value(face.pointOn)]
                            for face in faces), key=repr)
        return {'kind': 'geometry', 'members': shapes, 'face_geometry': face_data,
                'sides': {name: sorted((entity_key(item) for item in getattr(region, name, ())), key=repr)
                          for name in ('side1Faces', 'side2Faces', 'side12Faces') if getattr(region, name, ())}}
    return {'kind': 'mesh',
            'nodes': sorted(([entity_key(node), list(node.coordinates)] for node in getattr(region, 'nodes', ())), key=repr),
            'elements': sorted(([entity_key(elem), str(elem.type), [entity_key(node) for node in elem.getNodes()]]
                                for elem in getattr(region, 'elements', ())), key=repr),
            'sides': {name: sorted((entity_key(item) for item in getattr(region, name, ())), key=repr)
                      for name in ('face1Elements', 'face2Elements', 'face3Elements', 'face4Elements',
                                   'face5Elements', 'face6Elements') if getattr(region, name, ())}}


def reference_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [reference_value(item) for item in value]
    if hasattr(value, 'name'):
        return value.name
    return str(value)


def binding_keywords(model):
    """读取内存中的分析引用，不生成输入文件。"""
    keywords = {'STEP', 'END STEP', 'CONTACT', 'CONTACT INCLUSIONS', 'CONTACT EXCLUSIONS',
                'CONTACT PROPERTY ASSIGNMENT', 'CONTACT PAIR', 'CONTACT CONTROLS ASSIGNMENT',
                'RIGID BODY', 'TIE', 'COUPLING', 'EQUATION', 'MPC', 'BOUNDARY', 'CLOAD', 'DLOAD', 'DSLOAD',
                'DFLUX', 'FILM', 'RADIATE', 'AMPLITUDE', 'OUTPUT', 'CONTACT OUTPUT', 'NODE OUTPUT', 'ELEMENT OUTPUT'}
    block = model.keywordBlock
    block.synchVersions(storeNodesAndElements=False)
    found = []
    active = False
    for text in block.sieBlocks:
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('**')]
        for line in lines:
            if line.startswith('*'):
                active = line[1:].split(',', 1)[0].strip().upper() in keywords
                if active:
                    found.append([line])
            elif active:
                found[-1].append(line)
    return found


def bindings(model, part):
    """记录实际区域成员和 CAE 可读的引用。保持未知状态以供调用端验收。"""
    inventory = {}
    owners = [('part', part), ('assembly', model.rootAssembly)]
    owners.extend(('instance.' + name, instance) for name, instance in model.rootAssembly.instances.items()
                  if instance.partName == part.name)
    for scope, owner in owners:
        for kind in ('sets', 'surfaces', 'allInternalSets', 'allInternalSurfaces'):
            repo = getattr(owner, kind, {})
            for name in sorted(repo.keys()):
                inventory['%s.%s.%s' % (scope, kind, name)] = region_snapshot(repo[name])
    refs = {}
    unknown = []
    for kind in ('constraints', 'interactions', 'loads', 'boundaryConditions', 'historyOutputRequests'):
        for name, item in getattr(model, kind).items():
            key = '%s.%s' % (kind, name)
            fields = {attr: reference_value(getattr(item, attr))
                      for attr in ('region', 'main', 'secondary', 'master', 'slave', 'refPointRegion', 'bodyRegion',
                                   'pinRegion', 'tieRegion', 'sensor', 'variables', 'amplitude', 'suppressed')
                      if hasattr(item, attr)}
            for attr in ('includedPairs', 'excludedPairs', 'contactPropertyAssignments'):
                table = getattr(item, attr, None)
                if table is not None:
                    if hasattr(table, 'domainType'):
                        fields[attr + '.domainType'] = reference_value(table.domainType)
                    if hasattr(table, 'assignments'):
                        fields[attr + '.assignments'] = reference_value(table.assignments)
                    elif hasattr(table, 'pairs'):
                        fields[attr + '.pairs'] = reference_value(table.pairs)
                    elif not hasattr(table, 'domainType'):
                        unknown.append(key + '.' + attr)
            refs[key] = {'type': type(item).__name__, 'attributes': fields}
            if not fields or (kind != 'interactions' and not any(
                    attribute in fields for attribute in ('region', 'main', 'secondary', 'master', 'slave',
                                                          'refPointRegion', 'bodyRegion'))):
                unknown.append(key)
    try:
        keywords = binding_keywords(model)
    except Exception as exc:
        keywords = []
        unknown.append('关键字参照：' + str(exc))
    if any(lines[0].split(',', 1)[0].upper() == '*CONTACT' for lines in keywords):
        unknown = [key for key in unknown if not (
            key.rsplit('.', 1)[-1] in ('includedPairs', 'excludedPairs', 'contactPropertyAssignments')
            and refs.get(key.rsplit('.', 1)[0], {}).get('type') == 'ContactExp')]
    return {'regions': inventory, 'references': refs, 'keywords': keywords, 'unknown': sorted(set(unknown))}


def mesh_snapshot(model, part):
    check = part.verifyMeshQuality(criterion=ANALYSIS_CHECKS)
    names = ('warningElements', 'failedElements', 'naElements')
    if not all(name in check for name in names):
        raise RuntimeError('官方网格检查字段缺失。')
    try:
        stable = part.verifyMeshQuality(criterion=STABLE_TIME_INCREMENT)
        worst = stable.get('worstElement')
        step_info = {'worst': stable.get('worst'), 'average': stable.get('average'),
                     'worst_element': worst.label if worst is not None else None}
    except Exception as exc:
        step_info = {'error': str(exc)}
    return {'nodes': [[n.label] + list(n.coordinates) for n in part.nodes],
            'elements': [[e.label, str(e.type)] + [n.label for n in e.getNodes()] for e in part.elements],
            'warnings': sorted(e.label for e in check['warningElements']),
            'errors': sorted(e.label for e in check['failedElements']),
            'unavailable': sorted(e.label for e in check['naElements']),
            'stable_time': step_info, 'bindings': bindings(model, part),
            'incompatible_faces': [face.index for face in part.getIncompatibleMeshInterfaces()],
            'geometry_vertices': [reference_value(vertex.pointOn) for vertex in part.vertices],
            'cells': {str(cell.index): sorted(e.label for e in cell.getElements()) for cell in part.cells}}


def seed_snapshot(part):
    from abaqusConstants import (AVERAGE_SIZE, NUMBER, BIAS_RATIO, BIAS_METHOD, EDGE_SEEDING_METHOD,
                                BIAS_MIN_SIZE, BIAS_MAX_SIZE, SMALLEST_ELEM_LOCATION, VERTEX_ADJ_TO_SMALLEST_ELEM,
                                CONSTRAINT, DEVIATION_FACTOR, MIN_SIZE_FACTOR)
    attributes = (AVERAGE_SIZE, NUMBER, BIAS_RATIO, BIAS_METHOD, EDGE_SEEDING_METHOD,
                  BIAS_MIN_SIZE, BIAS_MAX_SIZE, SMALLEST_ELEM_LOCATION, VERTEX_ADJ_TO_SMALLEST_ELEM,
                  CONSTRAINT, DEVIATION_FACTOR, MIN_SIZE_FACTOR)
    return {str(edge.index): [reference_value(part.getEdgeSeeds(edge=edge, attribute=attr)) for attr in attributes]
            for edge in part.edges}


def named_elements(model, part, selector):
    kind, name = selector['value']
    repository = 'surfaces' if kind == 'surface' else 'sets'
    matches = [getattr(owner, repository)[name] for owner in (part, model.rootAssembly)
               if name in getattr(owner, repository)]
    if len(matches) != 1:
        raise ValueError('命名区域不存在或在部件和装配中重名。')
    region = matches[0]
    instances = {name for name, instance in model.rootAssembly.instances.items() if instance.partName == part.name}
    if kind == 'nset':
        nodes = {entity_key(node)[1] for node in region.nodes
                 if getattr(node, 'instanceName', None) in instances | {None, ''}}
        return [element.label for element in part.elements if any(node.label in nodes for node in element.getNodes())]
    elements = list(region.elements)
    if not elements:
        for item in list(getattr(region, 'faces', ())) + list(getattr(region, 'cells', ())):
            elements.extend(item.getElements())
    return sorted({element.label for element in elements if getattr(element, 'instanceName', None) in (None, '')
                   or element.instanceName in instances})


def native_selection(sequence, indices):
    words = [0] * (max(indices) // 32 + 1)
    for index in indices:
        words[index // 32] |= 1 << (index % 32)
    mask = '[' + ' '.join('#%x' % word for word in words) + ']'
    return sequence.getSequenceFromMask(mask=(mask,))


def run(request, result):
    openMdb(pathName=request['model_copy'])
    from abaqus import mdb
    model = mdb.models[request['model']]
    part = model.parts[request['part']]
    if not part.cells or not part.elements:
        raise ValueError('需要已有网格的原生实体部件。')
    mode = request['mode']
    if mode == 'probe':
        axis, offset = request['partition']
        plane = part.DatumPlaneByPrincipalPlane(principalPlane={'X': YZPLANE, 'Y': XZPLANE, 'Z': XYPLANE}[axis],
                                                offset=offset)
        count_before = len(part.cells)
        part.PartitionCellByDatumPlane(cells=part.cells[:], datumPlane=part.datums[plane.id])
        result['partition_probe'] = {'cells_before': count_before, 'cells_after': len(part.cells),
                                     'remaining_nodes': [n.label for n in part.nodes],
                                     'remaining_elements': [e.label for e in part.elements]}
        return
    if mode == 'apply':
        target = set(request['cell_ids'])
        selected = native_selection(part.cells, target)
        edge_ids = {i for cell in selected for i in cell.getEdges()}
        protected = {i for cell in part.cells if cell.index not in target for i in cell.getEdges()}
        editable = edge_ids - protected
        result['editable_edge_ids'] = sorted(editable)
        if not editable:
            raise ValueError('目标分区没有可独立布种的边。')
        edges = native_selection(part.edges, editable)
        part.deleteMesh(regions=(selected,))
        if request['algorithm'] != 'keep':
            flag = DEFAULT if request['algorithm'] == 'default' else NON_DEFAULT
            part.setMeshControls(regions=selected, algorithm=flag)
        size = request['seed_size']
        if request['bias'] == 1:
            part.seedEdgeBySize(edges=edges, size=size, constraint=FINER)
        else:
            # 仅按明确给出的轴向定位细端，避免猜测边的起止方向。
            axis, direction = request['bias_axis']
            end1, end2 = [], []
            for edge in edges:
                ends = edge.getVertices()
                if len(ends) != 2:
                    raise ValueError('偏置布种需要两端可定位的边。')
                p0, p1 = (part.vertices[i].pointOn[0][axis] for i in ends)
                if abs(p0 - p1) < 1e-9:
                    part.seedEdgeBySize(edges=part.edges[edge.index:edge.index + 1], size=size, constraint=FINER)
                else:
                    (end1 if (p0 < p1) == (direction == 'low') else end2).append(edge.index)
            for key, items in (('end1Edges', end1), ('end2Edges', end2)):
                if items:
                    part.seedEdgeByBias(biasMethod=SINGLE, minSize=size / request['bias'], maxSize=size,
                                        constraint=FINER, **{key: native_selection(part.edges, items)})
        part.generateMesh(regions=(selected,))
        if any(not part.cells[i].getElements() for i in target):
            raise RuntimeError('重划后存在空白实体分区。')
        model.rootAssembly.regenerate()
        mdb.save()
    result['mesh'] = mesh_snapshot(model, part)
    result['mesh']['seeds'] = seed_snapshot(part)
    selector = request.get('selector')
    if selector and selector['kind'] == 'named':
        result['mesh']['selected_labels'] = named_elements(model, part, selector)


if __name__ == '__main__':
    result = {'ok': False}
    try:
        with open('request.json') as stream:
            request = json.load(stream)
        run(request, result)
        result.update(ok=True, completed=True)
    except Exception:
        result['error'] = traceback.format_exc()
    save_result(result)
