"""V3.1 的网格结构、能量检查、时间预估和局部图。"""
import math
from pathlib import Path

from log_analysis import ROW, STEP, numeric, read_lines


def mesh_structure(parsed, part):
    nodes, elements, references = {}, [], []
    for block in parsed['records']['nodes']:
        if str(block['scope'].get('part', '')).casefold() != part.casefold(): continue
        for entry in block['data']:
            try:
                v=[x.strip() for x in entry['text'].split(',')]; nodes[int(v[0])] = tuple(float(x) for x in v[1:4])
            except (ValueError, IndexError): return {'status':'unknown', 'reason':'节点坐标格式不支持'}
        references.append(block['evidence'])
    for block in parsed['records']['elements']:
        if str(block['scope'].get('part', '')).casefold() != part.casefold(): continue
        for entry in block['data']:
            try:
                v=[int(x.strip()) for x in entry['text'].split(',') if x.strip()]; elements.append(v[1:])
            except ValueError: return {'status':'unknown', 'reason':'单元连接格式不支持'}
        references.append(block['evidence'])
    if not nodes or not elements or any(len(e)!=8 or any(n not in nodes for n in e) for e in elements):
        return {'status':'unknown', 'reason':'只统计具有完整八节点连接的规则网格', 'evidence':references}
    axes = [sorted({round(p[j],6) for p in nodes.values()}) for j in range(3)]
    maps=[{value:i for i,value in enumerate(axis)} for axis in axes]; occupied=set()
    for el in elements:
        spans=[]
        for j in range(3):
            indices=sorted({maps[j][round(nodes[n][j],6)] for n in el})
            if len(indices)!=2 or indices[1]!=indices[0]+1:
                return {'status':'unknown','reason':'网格非坐标轴对齐的规则六面体，需结合几何拓扑分析','evidence':references}
            spans.append(indices[0])
        occupied.add(tuple(spans))
    counts=[len(axis)-1 for axis in axes]
    if len(occupied)!=len(elements) or math.prod(counts)!=len(elements):
        return {'status':'unknown','reason':'网格存在缺口、重复或不规则分区','evidence':references}
    return {'status':'structured','coordinate_system':'Part 局部坐标','X':counts[0],'Y':counts[1],'Z':counts[2],
            'nodes':len(nodes),'elements':len(elements),'evidence':references}


def estimate(sta_path, step_number, target_time, count=20):
    path=Path(sta_path);lines,_=read_lines(path);step=None;rows=[]
    for line_no,text in enumerate(lines,1):
        match=STEP.match(text)
        if match: step=int(match.group(1))
        match=ROW.match(text)
        if step != step_number or not match: continue
        g=match.groups(); parts=g[3].split(':')
        wall=int(parts[0])*3600+int(parts[1])*60+float(parts[2])
        rows.append({'step_time':numeric(g[1]),'wall':wall,'increment':numeric(g[4]),'line':line_no})
    if len(rows)<2:return {'status':'unknown','reason':'同一步记录不足'}
    last=rows[-1]; prior=None
    for candidate in reversed(rows[max(0,len(rows)-count):-1]):
        if candidate['step_time']<last['step_time'] and candidate['wall']<last['wall']:
            prior=candidate
    if prior is None:return {'status':'unknown','reason':'近期时间记录没有有效增长'}
    speed=(last['step_time']-prior['step_time'])/(last['wall']-prior['wall'])
    remaining=max(0,target_time-last['step_time'])/speed
    return {'status':'estimate','current_time':last['step_time'],'target_time':target_time,
            'recent_simulated_seconds_per_wall_second':speed,'remaining_wall_seconds':remaining,
            'source_file':str(path),'first_line':prior['line'],'last_line':last['line'],
            'meaning':'基于最近记录速度外推，后续速度变化时应刷新'}


def plot_local(track, target_dir, step, target_time, destination):
    import csv,json
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from v3_geometry import _EDGES
    manifest=json.loads((Path(target_dir)/'target_manifest.json').read_text(encoding='utf-8'))
    frames=[f for f in track['frames'] if f['step']==step and f['step_time'] is not None]
    if not frames:return None
    frame=min(frames,key=lambda f:abs(f['step_time']-target_time))
    if abs(frame['step_time']-target_time)>0.0026:return None
    coords={n['label']:n['coordinates'] for n in manifest['nodes']}; displacements={}
    with (Path(target_dir)/'field.csv').open(encoding='utf-8',newline='') as f:
        for row in csv.DictReader(f):
            if row['step']==step and int(row['frame_index'])==frame['frame_index'] and row['variable']=='U' and row['node_label']:
                displacements[int(row['node_label'])]=json.loads(row['values_json'])
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
    shared=[]
    for label, xyz in coords.items():
        shared.append(xyz)
        if label in displacements and len(displacements[label]) >= 3:
            shared.append([xyz[j]+displacements[label][j] for j in range(3)])
    mins=[min(p[j] for p in shared) for j in range(3)];maxs=[max(p[j] for p in shared) for j in range(3)]
    radius=max(maxs[j]-mins[j] for j in range(3))/2 or 1
    fig=plt.figure(figsize=(10,4.5)); fig.suptitle('单元 %s 及邻域 | %s | t=%.5g s | 变形比例 1' % (track['element'],step,frame['step_time']))
    for col, deformed in enumerate((False,True),1):
        ax=fig.add_subplot(1,2,col,projection='3d'); ax.set_title('变形后' if deformed else '初始网格')
        all_pts=[]
        for e in manifest.get('neighborhood',[]):
            pts=[]
            for n in e['connectivity']:
                if n not in coords or (deformed and n not in displacements):pts=[];break
                pts.append([coords[n][j]+(displacements[n][j] if deformed else 0) for j in range(3)])
            if len(pts)!=8:continue
            all_pts.extend(pts);color='crimson' if e['label']==track['element'] else 'steelblue'
            for a,b in _EDGES:ax.plot([pts[a][0],pts[b][0]],[pts[a][1],pts[b][1]],[pts[a][2],pts[b][2]],color=color,linewidth=1.5 if color=='crimson' else .5)
        for j, setter in enumerate((ax.set_xlim,ax.set_ylim,ax.set_zlim)):
            center=(mins[j]+maxs[j])/2;setter(center-radius,center+radius)
        ax.set_xlabel('X');ax.set_ylabel('Y');ax.set_zlabel('Z');ax.view_init(22,-60)
    fig.tight_layout();fig.savefig(destination,dpi=150);plt.close(fig)
    return {'file':str(destination),'step':step,'frame_index':frame['frame_index'],'step_time':frame['step_time'],
            'data_source':str(Path(target_dir)/'field.csv'),'scale':1}
