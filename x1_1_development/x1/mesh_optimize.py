"""试验性四面体内节点平滑：固定所有外露边界，只输出独立 INP 候选。"""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from x1.core import digest, new_run
from x1.mesh_audit import read_inp, read_dat, boundary_inventory

MIN_VOLUME_RATIO = 0.25


def tetra_metrics(points):
    """矢量化体积、外接球质量与六条内部二面角；与 DAT 角度定义一致。"""
    p=np.asarray(points,dtype=float)
    signed=np.linalg.det(p[:,1:,:]-p[:,0:1,:])/6
    matrix=2*(p[:,1:,:]-p[:,0:1,:])
    rhs=np.sum((p[:,1:,:]-p[:,0:1,:])**2,axis=2)
    try:
        centers=np.linalg.solve(matrix,rhs[...,None])[...,0]
        radius=np.linalg.norm(centers,axis=1)
    except np.linalg.LinAlgError:
        raise ValueError('局部存在退化四面体。')
    if np.any(radius<=0): raise ValueError('四面体外接球无效。')
    quality=np.abs(signed)/(8*radius**3/(9*np.sqrt(3)))
    normals=[]
    for opposite in range(4):
        face=[i for i in range(4) if i!=opposite]
        normal=np.cross(p[:,face[1],:]-p[:,face[0],:],p[:,face[2],:]-p[:,face[0],:])
        flip=np.sum(normal*(p[:,opposite,:]-p[:,face[0],:]),axis=1)>0
        normal[flip]*=-1
        norm=np.linalg.norm(normal,axis=1)
        if np.any(norm==0): raise ValueError('四面体面退化。')
        normals.append(normal/norm[:,None])
    angles=[]
    for i in range(4):
        for j in range(i+1,4):
            angles.append(180-np.degrees(np.arccos(np.clip(np.sum(normals[i]*normals[j],axis=1),-1,1))))
    angles=np.asarray(angles).T
    return signed, quality, angles.min(axis=1), angles.max(axis=1)


def flags(quality,low,high):
    """仅用于排序候选；原始 DAT 和官方分析检查仍是最终依据。"""
    return (quality<0.02)|(low<10.0)|(high>160.0)


def penalty(quality,low,high):
    return (np.maximum(0,(.021-quality)/.021)**2 + np.maximum(0,(10.1-low)/10.1)**2 + np.maximum(0,(high-159.9)/20)**2).sum()


def smooth(coords, conn, boundary_indices, node_limit, passes=3, reference_volumes=None):
    """单节点局部搜索；固定边界、保持邻域已有合格单元及体积方向。"""
    xyz=coords.copy(); signed, quality, low, high=tetra_metrics(xyz[conn])
    volume_floor = MIN_VOLUME_RATIO * np.abs(signed)
    if reference_volumes is not None:
        reference_volumes = np.asarray(reference_volumes, dtype=float)
        if reference_volumes.shape != signed.shape or not np.isfinite(reference_volumes).all():
            raise ValueError('首轮单元体积记录无效。')
        volume_floor = np.maximum(volume_floor, MIN_VOLUME_RATIO * np.abs(reference_volumes))
        if np.any(signed * reference_volumes <= 0) or np.any(np.abs(signed) < volume_floor):
            raise ValueError('当前网格已触发首轮体积保护。')
    original_flags=flags(quality,low,high)
    stars=defaultdict(list)
    for i, tet in enumerate(conn):
        for node in tet: stars[int(node)].append(i)
    moves=[]
    rounds_completed=0
    for pass_no in range(passes):
      _,current_q,current_l,current_h=tetra_metrics(xyz[conn])
      current_flags=flags(current_q,current_l,current_h)
      scores=Counter()
      for index in np.flatnonzero(current_flags):
        for node in conn[index]:
          if int(node) not in boundary_indices: scores[int(node)]+=1
      ordered=sorted(scores,key=lambda n:(-scores[n],n))[:node_limit]
      pass_start=len(moves); rounds_completed+=1
      for node in ordered:
        star=np.asarray(stars[node],dtype=int); local_conn=conn[star]; start=xyz[node].copy()
        signed0,q0,l0,h0=tetra_metrics(xyz[local_conn]); old=flags(q0,l0,h0)
        if not np.any(old): continue
        neighbors=set(int(v) for v in local_conn.reshape(-1) if int(v)!=node)
        scale=min(float(np.linalg.norm(xyz[n]-start)) for n in neighbors)
        if scale<=0: continue
        bound=0.15*scale
        original_scale=min(float(np.linalg.norm(coords[n]-coords[node])) for n in neighbors)
        max_total_displacement=0.4*original_scale
        def objective(delta):
            xyz[node]=start+delta
            try:
                if np.linalg.norm(xyz[node]-coords[node])>max_total_displacement: return 1e9
                s,q,l,h=tetra_metrics(xyz[local_conn])
                if (np.any(s*signed0<=0) or np.any(np.abs(s)<np.abs(signed0)*0.1)
                        or np.any(np.abs(s)<volume_floor[star])): return 1e9
                new=flags(q,l,h)
                if np.any(new & ~old): return 1e8 + float(np.sum(new & ~old))
                return penalty(q,l,h) + .001*float(np.dot(delta,delta))/(bound*bound)
            except (ValueError, FloatingPointError):
                return 1e10
            finally:
                xyz[node]=start
        trial=minimize(objective,np.zeros(3),method='Powell',bounds=[(-bound,bound)]*3,
                       options={'maxiter':12,'maxfev':120,'xtol':1e-4,'ftol':1e-5})
        xyz[node]=start+trial.x
        try:
            s,q,l,h=tetra_metrics(xyz[local_conn]); new=flags(q,l,h)
            # 允许警告数暂时持平、但质量目标持续改善，为后续迭代留出空间。
            improves=(np.sum(new)<np.sum(old) or
                      (np.sum(new)==np.sum(old) and penalty(q,l,h)<penalty(q0,l0,h0)-1e-7))
            accepted=(np.linalg.norm(xyz[node]-coords[node])<=max_total_displacement
                      and np.all(s*signed0>0) and np.all(np.abs(s)>=np.abs(signed0)*0.1)
                      and np.all(np.abs(s)>=volume_floor[star])
                      and not np.any(new & ~old) and improves)
        except ValueError:
            accepted=False
        if accepted:
            moves.append({'node_index':int(node),'pass':pass_no+1,'distance':float(np.linalg.norm(trial.x)),
                          'before_local_warnings':int(np.sum(old)),'after_local_warnings':int(np.sum(new))})
        else: xyz[node]=start
      if len(moves)==pass_start: break
    signed1,q1,l1,h1=tetra_metrics(xyz[conn]); new_flags=flags(q1,l1,h1)
    if np.any(signed1*signed<=0) or np.any(np.abs(signed1)<volume_floor) or np.any(new_flags & ~original_flags):
        raise RuntimeError('全局安全检查未通过，拒绝输出网格。')
    if not np.array_equal(xyz[list(boundary_indices)],coords[list(boundary_indices)]):
        raise RuntimeError('外边界节点发生变化，拒绝输出网格。')
    return xyz,moves,original_flags,new_flags,rounds_completed


def verify_trial(before_path, after_path, part_name):
    """独立重读实际写出的 INP，验证连接、外边界和其他文本没有变化。"""
    before_path,after_path=Path(before_path).resolve(),Path(after_path).resolve()
    if before_path==after_path: raise ValueError('原始文件和候选文件必须是两个文件。')
    old,instances_old,_=read_inp(before_path); new,instances_new,_=read_inp(after_path)
    key=part_name.upper()
    if key not in old or key not in new: raise ValueError('目标部件不存在。')
    a,b=old[key],new[key]
    if set(a['nodes'])!=set(b['nodes']) or a['elements']!=b['elements']:
        raise ValueError('节点集合或单元连接不一致，拒绝视为内节点平滑。')
    boundary=boundary_inventory(a)
    if boundary is None or boundary['nonmanifold_faces']: raise ValueError('目标部件边界未知或存在非流形面。')
    moved=[n for n in a['nodes'] if a['nodes'][n][0]!=b['nodes'][n][0]]
    moved_boundary=[n for n in moved if n in boundary['nodes']]
    old_lines=before_path.read_bytes().splitlines(keepends=True); new_lines=after_path.read_bytes().splitlines(keepends=True)
    if len(old_lines)!=len(new_lines): raise ValueError('文件行数变化，拒绝视为纯节点修改。')
    expected_lines={a['nodes'][n][1] for n in moved}
    unexpected=[]
    for i,(x,y) in enumerate(zip(old_lines,new_lines),1):
        if x==y: continue
        if i not in expected_lines:
            unexpected.append(i); continue
        old_fields=x.rstrip(b'\r\n').split(b','); new_fields=y.rstrip(b'\r\n').split(b',')
        if len(old_fields)!=len(new_fields) or old_fields[0]!=new_fields[0] or old_fields[4:]!=new_fields[4:]:
            unexpected.append(i)
    nums=sorted(a['nodes']); indexes={label:i for i,label in enumerate(nums)}; els=sorted(a['elements'])
    conn=np.asarray([[indexes[n] for n in a['elements'][e]['nodes']] for e in els],dtype=int)
    p0=np.asarray([a['nodes'][n][0] for n in nums]);p1=np.asarray([b['nodes'][n][0] for n in nums])
    s0,q0,l0,h0=tetra_metrics(p0[conn]);s1,q1,l1,h1=tetra_metrics(p1[conn])
    bad0=flags(q0,l0,h0);bad1=flags(q1,l1,h1)
    result={'ok':not moved_boundary and not unexpected and not np.any(s0*s1<=0) and not np.any(np.abs(s1)<np.abs(s0)*0.25) and not np.any(bad1 & ~bad0) and instances_old==instances_new,
        'before':str(before_path),'after':str(after_path),'part':part_name,'before_sha256':digest(before_path),'after_sha256':digest(after_path),
        'moved_nodes':len(moved),'boundary_nodes_moved':len(moved_boundary),'connectivity_changed':False,
        'other_lines_changed':len(unexpected),'instance_definitions_equal':instances_old==instances_new,
        'volume_sign_changed':int(np.sum(s0*s1<=0)), 'volume_below_quarter_original':int(np.sum(np.abs(s1)<np.abs(s0)*0.25)),
        'independent_warning_before':int(bad0.sum()),
        'independent_warning_after':int(bad1.sum()),'newly_flagged_elements':int(np.sum(bad1 & ~bad0)),
        'status':'仅为独立几何和文本一致性检查；尚需 Abaqus 网格/数据检查及物理复核。'}
    return result


def mesh_optimize(inp,dat,part_name,max_nodes=2000,output_dir=None,passes=3):
    inp,dat=Path(inp).resolve(),Path(dat).resolve()
    if not inp.is_file() or not dat.is_file() or inp.suffix.lower()!='.inp' or dat.suffix.lower()!='.dat':
        raise ValueError('请指定存在的 INP 和 DAT。')
    if not 1<=max_nodes<=2000: raise ValueError('max-nodes 必须在 1 至 2000 之间。')
    if not 1<=passes<=50: raise ValueError('passes 必须在 1 至 50 之间。')
    root=Path(output_dir).resolve() if output_dir else Path(__file__).resolve().parents[1]/'runs'
    for source in (inp,dat):
        if root==source.parent or source.parent in root.parents: raise ValueError('输出目录需位于原始文件目录之外。')
    sha={'inp':digest(inp),'dat':digest(dat)}; parts,instances,limits=read_inp(inp)
    key=part_name.upper()
    if key not in parts: raise ValueError('目标部件未找到。')
    part=parts[key]; boundary=boundary_inventory(part)
    if boundary is None: raise ValueError('当前仅支持纯 C3D4/C3D4T 目标部件。')
    if boundary['nonmanifold_faces']: raise ValueError('检测到非流形面，停止平滑。')
    matching_instances=[name for name,x in instances.items() if x['part']==key]
    if len(matching_instances)!=1: raise ValueError('目标部件实例数不为 1，避免意外影响多个实例。')
    nodes=sorted(part['nodes']); node_index={label:i for i,label in enumerate(nodes)}
    elements=sorted(part['elements']); element_index={label:i for i,label in enumerate(elements)}
    xyz=np.asarray([part['nodes'][n][0] for n in nodes],dtype=float)
    conn=np.asarray([[node_index[n] for n in part['elements'][e]['nodes']] for e in elements],dtype=int)
    signed,q,lo,hi=tetra_metrics(xyz[conn]); targets=[r for r in read_dat(dat) if r['instance']==matching_instances[0] and r['table']=='tetrahedral']
    if not targets: raise ValueError('目标实例没有 DAT 四面体质量表。')
    for r in targets:
        if r['label'] not in element_index or r['adjusted']!='NO': raise ValueError('DAT 包含缺失单元或调整节点，停止优化。')
        i=element_index[r['label']]
        if abs(q[i]-r['values'][0])>2e-5 or abs(lo[i]-r['values'][1])>.005 or abs(hi[i]-r['values'][2])>.005:
            raise ValueError('INP 几何与 DAT 单元质量不一致，停止优化；请核对文件配对。')
    expected={r['label'] for r in targets}
    calculated={elements[i] for i in np.flatnonzero(flags(q,lo,hi))}
    if expected!=calculated:
        raise ValueError('独立指标的警告单元与 DAT 质量表不完全一致，停止优化并人工核对。')
    boundary_indices={node_index[n] for n in boundary['nodes']}
    after,moves,flags_before,flags_after,rounds_completed=smooth(xyz,conn,boundary_indices,max_nodes,passes)
    if {'inp':digest(inp),'dat':digest(dat)}!=sha: raise RuntimeError('输入文件在处理期间发生变化。')
    folder=new_run(root,'mesh_optimize'); output=None
    for item in moves:
        index=item['node_index']; after[index]=[float('%.15g'%v) for v in after[index]]
    rounded_signed,rounded_q,rounded_lo,rounded_hi=tetra_metrics(after[conn])
    rounded_flags=flags(rounded_q,rounded_lo,rounded_hi)
    if np.any(rounded_signed*signed<=0) or np.any(np.abs(rounded_signed)<np.abs(signed)*0.25) or np.any(rounded_flags & ~flags_before):
        raise RuntimeError('写出精度复核发现新增几何问题；已拒绝导出。')
    if np.sum(rounded_flags)>np.sum(flags_after):
        raise RuntimeError('写出精度改变了候选质量结果；已拒绝导出。')
    flags_after=rounded_flags
    moved={nodes[m['node_index']]:after[m['node_index']] for m in moves}
    if moved:
        raw=inp.read_bytes().splitlines(keepends=True)
        for label,coord in moved.items():
            line=part['nodes'][label][1]-1
            text=raw[line].decode('latin-1'); ending='\r\n' if text.endswith('\r\n') else '\n' if text.endswith('\n') else ''
            values=text.rstrip('\r\n').split(',')
            if len(values)<4 or int(values[0].strip())!=label: raise RuntimeError('节点行映射发生变化。')
            values[1:4]=[' %.15g'%v for v in coord]
            raw[line]=(','.join(values)+ending).encode('latin-1')
        output=folder/(inp.stem+'_mesh_trial.inp'); output.write_bytes(b''.join(raw))
    with (folder/'移动节点.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=['node_label','inp_line','pass','distance','before_local_warnings','after_local_warnings']);writer.writeheader()
        for item in moves:
            writer.writerow({'node_label':nodes[item['node_index']],'inp_line':part['nodes'][nodes[item['node_index']]][1], 'pass':item['pass'],
                             'distance':item['distance'],'before_local_warnings':item['before_local_warnings'],
                             'after_local_warnings':item['after_local_warnings']})
    warning_lines={entry['label']:entry['dat_line'] for entry in targets}
    with (folder/'单元对照.csv').open('w',newline='',encoding='utf-8-sig') as f:
        fields=['element_label','inp_line','dat_line','warning_before','warning_after','quality_before','quality_after',
                'min_dihedral_before','min_dihedral_after','max_dihedral_before','max_dihedral_after']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for i in np.flatnonzero(flags_before | flags_after):
            label=elements[i]
            writer.writerow({'element_label':label,'inp_line':part['elements'][label]['line'],
                'dat_line':warning_lines.get(label),'warning_before':bool(flags_before[i]), 'warning_after':bool(flags_after[i]),
                'quality_before':float(q[i]),'quality_after':float(rounded_q[i]),
                'min_dihedral_before':float(lo[i]),'min_dihedral_after':float(rounded_lo[i]),
                'max_dihedral_before':float(hi[i]),'max_dihedral_after':float(rounded_hi[i])})
    verification=verify_trial(inp,output,part_name) if output else None
    if verification and not verification['ok']: raise RuntimeError('实际候选文件一致性复核失败。')
    result={'ok':True,'version':'X1.4','input':{'inp':str(inp),'dat':str(dat),'sha256':sha},'job':inp.stem,'part':part_name,
        'instance':matching_instances[0],'source_unchanged':{'inp':digest(inp),'dat':digest(dat)}==sha,
        'candidate_inp':str(output) if output else None,'candidate_sha256':digest(output) if output else None,
        'boundary_nodes_fixed':len(boundary_indices),'moved_nodes':len(moved),'accepted_moves':len(moves),
        'evaluated_nodes_limit_per_pass':max_nodes,'passes_completed':rounds_completed,
        'independent_warning_before':int(flags_before.sum()),'independent_warning_after':int(flags_after.sum()),
        'newly_flagged_elements':int(np.sum(flags_after & ~flags_before)), 'candidate_verification':verification,
        'matching_status':'几何与 DAT 质量表数值已比对；生成来源仍需独立核对。',
        'status':'实验性 INP 候选：尚未执行 Abaqus 数据检查或求解；结果未同步至 CAE。',
        'limitations':limits+['边界按外露单元面识别，未更改边界节点或单元连接。','内部单元面、节点载荷、耦合、约束和内部集合依赖未全面识别；节点移动可能影响它们，须逐项复核。','外表面保持不等同接触建模或热力结果通过。','稳定时间增量、分析检查和物理响应须使用 Abaqus 复核。'],
        'report':str(folder/'报告.md'),'moved_nodes_csv':str(folder/'移动节点.csv'), 'element_comparison_csv':str(folder/'单元对照.csv')}
    (folder/'结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# X1.4 内节点网格优化试验','',f'输入 INP：`{inp}`；DAT：`{dat}`；作业：`{inp.stem}`；部件：`{part_name}`。','',
           f'独立指标警告：{result["independent_warning_before"]} → {result["independent_warning_after"]}；新增警告：{result["newly_flagged_elements"]}。',
           f'固定外边界节点：{result["boundary_nodes_fixed"]}；移动内部节点：{result["moved_nodes"]}。',
           f'候选文件：`{output}`。', '',result['status'], '', '**限制：**']
    lines.extend('- '+x for x in result['limitations'])
    (folder/'报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result
