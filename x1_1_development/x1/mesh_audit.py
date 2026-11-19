"""X1.4 网格审计：只读 INP/DAT，保留源文件行号和计算覆盖范围。"""
import csv
import json
import math
import re
from collections import Counter, defaultdict, deque
from pathlib import Path

import numpy as np

from x1.core import digest, new_run


def _params(line):
    fields = [x.strip() for x in line[1:].split(',')]
    result = {}
    for field in fields[1:]:
        if '=' in field:
            key, value = field.split('=', 1)
            result[key.strip().lower()] = value.strip()
    return fields[0].lower(), result


def read_inp(path):
    """读取显式单行 C3D4(T) 和 C3D8 系列；未知语义列入覆盖限制。"""
    path = Path(path)
    parts, instances, limitations = {}, {}, []
    part = mode = elem_type = None
    for number, raw in enumerate(path.read_text(encoding='latin-1').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('**'):
            continue
        if line.startswith('*'):
            key, par = _params(line)
            mode = None
            if key == 'include':
                raise ValueError('INP 包含 INCLUDE；为避免遗漏网格，请先提供完整展开的独立输入。')
            if key == 'part':
                part = par.get('name', '').upper()
                if not part or part in parts:
                    raise ValueError('部件名称缺失或重复，无法可靠映射网格。')
                parts[part] = {'name': par['name'], 'line': number, 'nodes': {}, 'elements': {}, 'unsupported_mesh': []}
            elif key == 'end part':
                part = None
            elif key == 'instance':
                if 'name' in par and 'part' in par:
                    instances[par['name'].upper()] = {'part': par['part'].upper(), 'line': number}
                mode = 'instance_transform'
            elif key == 'node' and part:
                if par.get('system', 'R').upper() != 'R':
                    raise ValueError('第 %s 行采用非直角节点坐标系，暂不支持几何计算。' % number)
                mode = 'nodes'
            elif key == 'element' and part:
                mode, elem_type = 'elements', par.get('type', '').upper()
            elif part and key in ('ngen','nfill','ncopy','elgen','elcopy'):
                parts[part]['unsupported_mesh'].append((key,number))
                limitations.append('部件 %s 使用 %s 生成网格，第 %s 行未展开。' % (part,key,number))
            elif part and key == 'system':
                raise ValueError('第 %s 行使用局部坐标变换，暂不支持安全几何计算。' % number)
            continue
        if mode == 'instance_transform':
            limitations.append('实例变换存在：报告坐标使用部件局部坐标，未转换到装配坐标。')
            mode = None
        if not part or mode not in ('nodes', 'elements'):
            continue
        values = [x.strip() for x in line.split(',') if x.strip()]
        try:
            if mode == 'nodes' and len(values) >= 4:
                label = int(values[0]); coords = [float(v.replace('D', 'E').replace('d', 'e')) for v in values[1:4]]
                if label in parts[part]['nodes']:
                    raise ValueError('重复节点编号')
                parts[part]['nodes'][label] = (coords, number)
            elif mode == 'elements':
                count = 4 if elem_type in ('C3D4', 'C3D4T') else 8 if elem_type.startswith('C3D8') else None
                if count is None:
                    msg = '单元类型 %s 不在几何计算覆盖范围。' % elem_type
                    if msg not in limitations: limitations.append(msg)
                    marker=('element type '+elem_type,number)
                    if not any(x[0]==marker[0] for x in parts[part]['unsupported_mesh']):
                        parts[part]['unsupported_mesh'].append(marker)
                    continue
                if len(values) != count + 1:
                    raise ValueError('第 %s 行单元连接格式不受支持（含续行）；未生成不完整报告。' % number)
                label = int(values[0])
                if label in parts[part]['elements']: raise ValueError('重复单元编号')
                parts[part]['elements'][label] = {'type': elem_type, 'nodes': [int(v) for v in values[1:]], 'line': number}
        except (TypeError, ValueError) as exc:
            raise ValueError('INP 第 %s 行解析失败：%s' % (number, exc)) from exc
    if not parts or not instances:
        raise ValueError('缺少可识别的部件或装配实例；本版本只支持部件/实例式网格。')
    return parts, instances, sorted(set(limitations))


def read_dat(path):
    """读取 DAT 中的网格质量表；保留质量表实际行号。"""
    row = re.compile(r'^\s*(\S+)\.(\d+)\s+(.+?)\s+(YES|NO)\s*$', re.I)
    entries, table = [], None
    for n, raw in enumerate(Path(path).read_text(encoding='latin-1').splitlines(), 1):
        if re.search(r'Distorted tetrahedral elements', raw, re.I): table = 'tetrahedral'
        elif re.search(r'Distorted isoparametric elements', raw, re.I): table = 'isoparametric'
        elif '----' in raw and entries and not raw.strip().startswith('-'): table = None
        if not table: continue
        hit = row.match(raw)
        if not hit: continue
        try:
            nums = [float(x.replace('D','E').replace('d','e')) for x in hit.group(3).split()]
        except ValueError:
            continue
        if table == 'tetrahedral' and len(nums) != 3: continue
        if table == 'isoparametric' and len(nums) != 1: continue
        entries.append({'instance': hit.group(1).upper(), 'label': int(hit.group(2)), 'table': table,
                        'values': nums, 'adjusted': hit.group(4).upper(), 'dat_line': n})
    return entries


def tet_geometry(coords):
    """独立计算四面体体积、棱长、内部二面角和外接球归一质量。"""
    p = np.asarray(coords, dtype=float)
    volume = abs(float(np.linalg.det(np.stack((p[1]-p[0], p[2]-p[0], p[3]-p[0]))))) / 6.0
    lengths = [float(np.linalg.norm(p[i]-p[j])) for i in range(4) for j in range(i+1, 4)]
    normals = []
    for opposite in range(4):
        face = [i for i in range(4) if i != opposite]
        normal = np.cross(p[face[1]]-p[face[0]], p[face[2]]-p[face[0]])
        if np.dot(normal, p[opposite]-p[face[0]]) > 0: normal = -normal
        norm = float(np.linalg.norm(normal))
        if norm == 0: return {'volume': volume, 'min_edge': min(lengths), 'max_edge': max(lengths), 'min_dihedral': None, 'max_dihedral': None, 'quality': None}
        normals.append(normal / norm)
    angles = [180.0-math.degrees(math.acos(float(np.clip(np.dot(normals[i], normals[j]), -1, 1)))) for i in range(4) for j in range(i+1, 4)]
    try:
        relative = p[1:]-p[0]
        center = np.linalg.solve(2*relative, np.sum(relative**2, axis=1))
        radius = float(np.linalg.norm(center))
        quality = volume / (8*radius**3/(9*math.sqrt(3))) if radius > 0 else None
    except np.linalg.LinAlgError:
        quality = None
    return {'volume': volume, 'min_edge': min(lengths), 'max_edge': max(lengths), 'min_dihedral': min(angles), 'max_dihedral': max(angles), 'quality': quality}


def warning_components(rows):
    """以共享四面体面分组；仅包含 DAT 中列出的警告单元。"""
    faces, graph = defaultdict(list), defaultdict(set)
    for i, row in enumerate(rows):
        if row['type'] not in ('C3D4', 'C3D4T'): continue
        nodes = row['_nodes']
        for excluded in range(4): faces[(row['instance'], tuple(sorted(nodes[:excluded]+nodes[excluded+1:])))].append(i)
    for linked in faces.values():
        for a in linked:
            graph[a].update(b for b in linked if b != a)
    seen, groups = set(), []
    for i in range(len(rows)):
        if rows[i]['type'] not in ('C3D4', 'C3D4T') or i in seen: continue
        queue = deque([i]); seen.add(i); group = []
        while queue:
            current = queue.popleft(); group.append(current)
            for other in graph[current]:
                if other not in seen: seen.add(other); queue.append(other)
        groups.append(group)
    groups.sort(key=len, reverse=True)
    for no, group in enumerate(groups, 1):
        for i in group: rows[i]['component'] = no
    return [len(g) for g in groups]


def boundary_inventory(part):
    """仅对纯四面体网格识别外边界；混合网格标为未知。"""
    elements=part['elements'].values()
    if part.get('unsupported_mesh') or not elements or any(e['type'] not in ('C3D4','C3D4T') for e in elements):
        return None
    counts=Counter()
    for el in elements:
        n=el['nodes']
        for skip in range(4): counts[tuple(sorted(n[:skip]+n[skip+1:]))]+=1
    surface_nodes=set()
    for face,count in counts.items():
        if count==1: surface_nodes.update(face)
    return {'nodes':surface_nodes, 'nonmanifold_faces':sum(count>2 for count in counts.values())}


def mesh_audit(inp, dat, part_filter=None, output_dir=None):
    inp, dat = Path(inp).resolve(), Path(dat).resolve()
    if not inp.is_file() or not dat.is_file(): raise ValueError('指定 INP 或 DAT 不存在。')
    if inp.suffix.lower() != '.inp' or dat.suffix.lower() != '.dat': raise ValueError('请指定 INP 与 DAT 文件。')
    output_root = Path(output_dir).resolve() if output_dir else Path(__file__).resolve().parents[1] / 'runs'
    for source in (inp, dat):
        if output_root == source.parent or source.parent in output_root.parents:
            raise ValueError('输出目录需位于原始输入目录之外。')
    before = {'inp': digest(inp), 'dat': digest(dat)}
    parts, instances, limits = read_inp(inp)
    dat_rows = read_dat(dat)
    if not dat_rows: raise ValueError('DAT 未找到受支持的畸变网格表；请核对文件和作业。')
    rows, unmapped, boundaries = [], [], {}
    for item in dat_rows:
        inst = instances.get(item['instance'])
        if not inst or inst['part'] not in parts:
            unmapped.append(item); continue
        part = parts[inst['part']]
        if part_filter and part['name'].casefold() != part_filter.casefold(): continue
        el = part['elements'].get(item['label'])
        if not el or any(n not in part['nodes'] for n in el['nodes']):
            unmapped.append(item); continue
        p = np.asarray([part['nodes'][n][0] for n in el['nodes']])
        centroid = p.mean(axis=0)
        row = {'instance': item['instance'], 'part': part['name'], 'label': item['label'], 'type': el['type'],
               'x': float(centroid[0]), 'y': float(centroid[1]), 'z': float(centroid[2]),
               'inp_element_line': el['line'], 'dat_line': item['dat_line'], 'adjusted': item['adjusted'],
               'dat_quality': item['values'][0] if item['table']=='tetrahedral' else None,
               'dat_min_angle': item['values'][1] if item['table']=='tetrahedral' else item['values'][0],
               'dat_max_angle': item['values'][2] if item['table']=='tetrahedral' else None, '_nodes': el['nodes']}
        if item['table'] == 'tetrahedral' and el['type'] in ('C3D4', 'C3D4T'):
            row.update(tet_geometry(p))
            row['independent_vs_dat_max_angle_error_deg'] = max(abs(row['min_dihedral']-row['dat_min_angle']), abs(row['max_dihedral']-row['dat_max_angle'])) if row['min_dihedral'] is not None else None
            if inst['part'] not in boundaries: boundaries[inst['part']]=boundary_inventory(part)
            boundary=boundaries[inst['part']]
            if boundary is not None:
                row['boundary_nodes']=sum(n in boundary['nodes'] for n in el['nodes'])
                row['interior_nodes']=4-row['boundary_nodes']
        rows.append(row)
    if part_filter and part_filter.casefold() not in [p['name'].casefold() for p in parts.values()]:
        raise ValueError('指定部件不存在于 INP：%s' % part_filter)
    components = warning_components(rows)
    for row in rows: row.pop('_nodes', None)
    after = {'inp': digest(inp), 'dat': digest(dat)}
    if before != after: raise RuntimeError('审计过程中输入文件发生变化；结果已拒绝。')
    folder = new_run(output_root, 'mesh_audit')
    fields = ['instance','part','label','type','component','x','y','z','inp_element_line','dat_line','adjusted','dat_quality','dat_min_angle','dat_max_angle','boundary_nodes','interior_nodes','volume','min_edge','max_edge','min_dihedral','max_dihedral','quality','independent_vs_dat_max_angle_error_deg']
    with (folder/'警告单元.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f, fieldnames=fields, extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
    tetra_rows=[r for r in rows if 'volume' in r]
    summary={'min_dihedral_below_10':sum(r['dat_min_angle']<10 for r in tetra_rows),
             'max_dihedral_above_160':sum(r['dat_max_angle']>160 for r in tetra_rows),
             'quality_below_0_02':sum(r['dat_quality']<0.02 for r in tetra_rows),
             'all_four_nodes_on_boundary':sum(r.get('boundary_nodes')==4 for r in tetra_rows),
             'boundary_coverage':sum('boundary_nodes' in r for r in tetra_rows),
             'worst_dat_min_dihedral':min((r['dat_min_angle'] for r in tetra_rows),default=None),
             'max_independent_angle_difference_deg':max((r['independent_vs_dat_max_angle_error_deg'] for r in tetra_rows if r.get('independent_vs_dat_max_angle_error_deg') is not None),default=None),
             'max_independent_quality_difference':max((abs(r['quality']-r['dat_quality']) for r in tetra_rows if r.get('quality') is not None),default=None)}
    result = {'ok': True, 'version': 'X1.4', 'input': {'inp':str(inp),'dat':str(dat),'sha256':before},
              'job':inp.stem, 'part_filter':part_filter, 'source_unchanged':before==after,
              'dat_listed_total':len(dat_rows), 'selected_mapped':len(rows), 'unmapped':len(unmapped),
              'instances':dict(Counter(r['instance'] for r in rows)), 'types':dict(Counter(r['type'] for r in rows)),
              'tet_face_connected_component_count':len(components), 'tet_face_connected_largest_20':components[:20],
              'tet_geometry_coverage':len(tetra_rows), 'tet_quality_summary':summary,
              'matching_status':'未验证 INP 与 DAT 的生成对应关系；同名或内容可映射并非证明。',
              'limitations':limits+['CAE 未保存的修改不在输入文件中。','四面体几何指标为独立计算；最终网格分析检查以 Abaqus 为准。', '局部坐标为 INP 部件坐标。', '边界由网格外露面识别，非物理接触面分类；拓扑不流形或重叠网格可能影响结果。', '仅列出 DAT 质量表中实际列出的单元；其余警告需另查。'],
              'csv':str(folder/'警告单元.csv'), 'report':str(folder/'报告.md')}
    (folder/'结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    report = ['# X1.4 网格审计','',f'输入 INP：`{inp}`；DAT：`{dat}`；作业名：`{inp.stem}`。','',
              f'DAT 质量表共列出 {len(dat_rows)} 个单元，目标范围匹配 {len(rows)} 个，未映射 {len(unmapped)} 个。',
              f'实例分布：{result["instances"]}；单元类型：{result["types"]}。',
              f'四面体共享面连通分组大小：{components[:20]}。','',
              f'四面体最小二面角低于 10° 的有 {summary["min_dihedral_below_10"]} 个；最大二面角大于 160° 的有 {summary["max_dihedral_above_160"]} 个；质量指标低于 0.02 的有 {summary["quality_below_0_02"]} 个；条件可重叠。',
              f'可识别边界的 {summary["boundary_coverage"]} 个四面体中，四个节点均在网格外边界的有 {summary["all_four_nodes_on_boundary"]} 个。',
              f'最小二面角：{summary["worst_dat_min_dihedral"]}°；独立角度计算与 DAT 最大差：{summary["max_independent_angle_difference_deg"]}°。',
              '单元坐标、DAT/INP 行号和独立几何计算见 `警告单元.csv`。','',
              '**关联状态：** '+result['matching_status'],'','**限制：**']
    report.extend('- '+x for x in result['limitations'])
    (folder/'报告.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    return result
