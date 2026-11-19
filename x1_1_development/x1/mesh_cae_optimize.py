"""对 CAE 独立副本执行四面体内节点优化与官方网格检查。"""
import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from collections import Counter

import numpy as np

from x1.core import digest, new_run
from x1.mesh_optimize import MIN_VOLUME_RATIO, smooth, tetra_metrics, flags


def prepare_edits(data, max_nodes=2000, passes=10, reference=None):
    """官方与独立警告集合严格一致时才生成内节点候选。"""
    if not data.get('ok'): raise ValueError('CAE 网格导出失败。')
    if data.get('errors') or data.get('unavailable'): raise ValueError('官方检查含错误或未检查单元。')
    if len(data.get('instances',[]))!=1: raise ValueError('目标部件实例数不是 1，停止试验。')
    nodes=data['nodes']; elements=data['elements']
    if any(len(e)!=6 or e[1].upper() not in ('C3D4','C3D4T') for e in elements):
        raise ValueError('仅支持纯 C3D4/C3D4T 四面体网格。')
    if len({n[0] for n in nodes})!=len(nodes) or len({e[0] for e in elements})!=len(elements):
        raise ValueError('节点或单元编号重复。')
    labels=[n[0] for n in nodes]; bylabel={n:i for i,n in enumerate(labels)}
    xyz=np.asarray([n[1:4] for n in nodes],dtype=float)
    conn=np.asarray([[bylabel[n] for n in e[2:]] for e in elements],dtype=int)
    signed,q,lo,hi=tetra_metrics(xyz[conn]); baseline=flags(q,lo,hi)
    flagged={elements[i][0] for i in np.flatnonzero(baseline)}
    if flagged!=set(data['warnings']): raise ValueError('独立几何指标与 Abaqus 官方警告集合不一致，停止试验。')
    faces=Counter()
    for tet in conn:
        t=list(map(int,tet))
        for i in range(4): faces[tuple(sorted(t[:i]+t[i+1:]))]+=1
    if any(n>2 for n in faces.values()): raise ValueError('发现非流形面，停止试验。')
    boundary={n for face,count in faces.items() if count==1 for n in face}
    reference_signed = signed
    if reference is not None:
        reference_nodes = {node[0]: node[1:] for node in reference['nodes']}
        reference_xyz = np.asarray([reference_nodes[label] for label in labels])
        reference_signed = tetra_metrics(reference_xyz[conn])[0]
    adjusted,moves,before,after,rounds=smooth(
        xyz,conn,boundary,max_nodes,passes,reference_volumes=reference_signed)
    indices=sorted({m['node_index'] for m in moves})
    edits=[{'label':labels[i],'before':xyz[i].tolist(),'after':adjusted[i].tolist()} for i in indices]
    affected = np.flatnonzero(np.isin(conn, indices).any(axis=1))
    volume_checks = []
    for index in affected:
        current, original = float(signed[index]), float(reference_signed[index])
        volume_checks.append([elements[index][0], elements[index][2:], current,
                              MIN_VOLUME_RATIO * max(abs(current), abs(original)), original])
    return {'edits':edits,'volume_checks':volume_checks,'boundary_count':len(boundary),'independent_before':int(before.sum()),
            'independent_after':int(after.sum()),'independent_new':int(np.sum(after & ~before)),
            'passes':rounds}


def _run_worker(abaqus, folder, worker, request, recover_completed=False):
    (folder/'request.json').write_text(json.dumps(request,ensure_ascii=True,indent=2),encoding='ascii')
    command=[abaqus,'cae','noGUI='+str(Path(__file__).parent/worker)]
    try:
        process=subprocess.run(command,cwd=str(folder),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               timeout=900,shell=(os.name=='nt'))
        raw=process.stdout or b''; code=process.returncode
    except (OSError,subprocess.TimeoutExpired) as exc:
        raw=(getattr(exc,'output',None) or b'')+str(exc).encode('utf-8'); code=None
    (folder/'后台日志.raw').write_bytes(raw)
    for encoding in dict.fromkeys(('utf-8',locale.getpreferredencoding(False),'gb18030','latin-1')):
        try:
            output=raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    (folder/'后台日志.txt').write_text(output,encoding='utf-8')
    abnormal_message=re.search(r'cae exited with an error code\s+(\d+)',output,re.I)
    if code==0 and abnormal_message:
        code=int(abnormal_message[1])
    path=folder/'worker_result.json'
    try:
        result=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'ok':False,'error':output.strip() or '内核未返回结果'}
    except (OSError,ValueError) as exc:
        result={'ok':False,'error':'内核结果不完整：'+str(exc)}
    result['returncode']=code
    result['abnormal_exit']=code not in (0,None)
    if code!=0 and not (recover_completed and code is not None and (code & 0xffffffff) == 0xc0000409
                        and result.get('completed') and result.get('ok')):
        result['ok']=False
    return result


class VolumeGuardError(ValueError):
    def __init__(self, failures):
        super().__init__('候选单元体积检查失败，共 %s 个。' % len(failures))
        self.failures=failures


def verify_exports(before, after, require_improvement=True):
    """重新读取保存的候选，复查节点、连接、外边界及官方网格结果。"""
    if not after.get('ok'): raise ValueError('保存候选的重新读取失败。')
    old_nodes={n[0]:n[1:] for n in before['nodes']}; new_nodes={n[0]:n[1:] for n in after['nodes']}
    old_elements={e[0]:e[1:] for e in before['elements']}; new_elements={e[0]:e[1:] for e in after['elements']}
    if old_nodes.keys()!=new_nodes.keys() or old_elements!=new_elements or before['instances']!=after['instances']:
        raise ValueError('候选节点集合、单元连接或实例发生变化。')
    counts=Counter()
    for e in before['elements']:
        n=e[2:]
        for i in range(4): counts[tuple(sorted(n[:i]+n[i+1:]))]+=1
    if any(v>2 for v in counts.values()): raise ValueError('存在非流形面。')
    boundary={n for face,count in counts.items() if count==1 for n in face}
    moved={n for n in old_nodes if old_nodes[n]!=new_nodes[n]}
    if moved & boundary: raise ValueError('候选外边界节点发生变化。')
    labels=sorted(old_nodes); indices={n:i for i,n in enumerate(labels)}
    conn=np.asarray([[indices[n] for n in e[2:]] for e in before['elements']],dtype=int)
    p0=np.asarray([old_nodes[n] for n in labels]);p1=np.asarray([new_nodes[n] for n in labels])
    s0,q0,l0,h0=tetra_metrics(p0[conn]);s1,q1,l1,h1=tetra_metrics(p1[conn])
    invalid=np.flatnonzero((s0*s1<=0)|(np.abs(s1)<np.abs(s0)*MIN_VOLUME_RATIO))
    if len(invalid):
        element_labels=list(old_elements)
        raise VolumeGuardError([{'label':element_labels[i],'nodes':old_elements[element_labels[i]][1:],
            'baseline_signed_volume':float(s0[i]),'candidate_signed_volume':float(s1[i]),
            'ratio':float(abs(s1[i]/s0[i])) if s0[i] else None} for i in invalid])
    old_bad=set(before['warnings']);new_bad=set(after['warnings'])
    if after['errors'] or after['unavailable'] or new_bad-old_bad or (require_improvement and len(new_bad)>=len(old_bad)):
        raise ValueError('候选官方网格检查未通过安全门。')
    return {'moved_nodes':len(moved),'boundary_nodes_moved':len(moved & boundary),'connectivity_unchanged':True,
            'volume_sign_changes':int(np.sum(s0*s1<=0)),'official_before':len(old_bad),'official_after':len(new_bad),
            'min_volume_ratio':float(np.min(np.abs(s1/s0))),
            'official_new_warnings':len(new_bad-old_bad),'independent_before':int(flags(q0,l0,h0).sum()),
            'independent_after':int(flags(q1,l1,h1).sum()),
            'stable_time_before':before.get('stable_time',{}), 'stable_time_after':after.get('stable_time',{}),
            'status':'仅验证目标网格；模型引用及物理结果仍待复核。'}


def original_export_for_candidate(source):
    """追踪本工具生成的连续候选，始终沿用首轮优化前的网格作为安全基准。"""
    current=Path(source).resolve()
    seen=set()
    earliest=None
    explicit_reference=None
    while True:
        if current in seen: raise ValueError('候选模型来源存在循环。')
        seen.add(current)
        record_path=current.parent.parent/'结果.json'
        if current.name!='model_candidate.cae' or current.parent.name!='apply' or not record_path.is_file():
            break
        record=json.loads(record_path.read_text(encoding='utf-8'))
        recorded=record.get('candidate')
        if not record.get('ok') or not recorded or Path(recorded).resolve()!=current:
            raise ValueError('候选模型来源记录无效，已停止连续优化。')
        if record.get('candidate_sha256')!=digest(current):
            raise ValueError('候选模型与来源记录的哈希不一致，已停止连续优化。')
        export_path=record_path.parent/'export'/'worker_result.json'
        recorded_input=Path(record['input']).resolve()
        if not export_path.is_file() or not recorded_input.is_file() or digest(recorded_input)!=record.get('input_sha256'):
            raise ValueError('原始基准文件或网格记录缺失或已变更，已停止连续优化。')
        explicit=record.get('explicit_baseline')
        if explicit:
            reference_path=Path(explicit['path'])
            if not reference_path.is_file() or digest(reference_path)!=explicit['sha256']:
                raise ValueError('累计体积的显式基准文件缺失或已变更。')
            reference=json.loads(reference_path.read_text(encoding='utf-8-sig'))
            reference=reference.get('mesh',reference)
            if explicit_reference is not None and explicit_reference!=reference:
                raise ValueError('候选来源含有不一致的显式体积基准。')
            explicit_reference=reference
        earliest=json.loads(export_path.read_text(encoding='utf-8'))
        current=Path(record['input']).resolve()
    return explicit_reference if explicit_reference is not None else earliest


def cae_mesh_optimize(cae,model,part,abaqus='abaqus',output_dir=None,max_nodes=2000,passes=10,baseline=None):
    source=Path(cae).resolve()
    if not source.is_file() or source.suffix.lower()!='.cae': raise ValueError('请提供已有 CAE 文件。')
    if not 1<=max_nodes<=2000 or not 1<=passes<=50: raise ValueError('参数范围错误。')
    root=Path(output_dir).resolve() if output_dir else Path(__file__).resolve().parents[1]/'runs'
    if root==source.parent or source.parent in root.parents: raise ValueError('输出目录必须位于原始 CAE 目录之外。')
    initial_hash=digest(source); started=time.perf_counter(); folder=new_run(root,'cae_mesh_optimize')
    result={'ok':False,'version':'X1.6','input':str(source),'input_sha256':initial_hash,'model':model,'part':part,
            'job':'未启动作业','run_dir':str(folder),'candidate':None,
            'limitations':['未知内部接触面、节点载荷或集合依赖，需人工复核。','网格检查结果不等于热力学分析通过。','后台读取的是磁盘 CAE，未保存的界面修改不包含在内。']}
    try:
        inspect=folder/'export'; inspect.mkdir(); export_copy=inspect/'model_working.cae'; shutil.copy2(source,export_copy)
        exported=_run_worker(abaqus,inspect,'worker_cae_export.py',{'model_copy':str(export_copy),'model':model,'part':part},
                             recover_completed=True)
        result['worker_returncodes']={'export':exported.get('returncode')}
        if not exported.get('ok'): raise RuntimeError('CAE 导出未完成：'+exported.get('error','未知'))
        root_export=original_export_for_candidate(source)
        if baseline:
            baseline_path=Path(baseline).resolve()
            explicit=json.loads(baseline_path.read_text(encoding='utf-8-sig'))
            explicit=explicit.get('mesh',explicit)
            if root_export is not None and (root_export['nodes']!=explicit['nodes'] or root_export['elements']!=explicit['elements']):
                raise ValueError('显式基准和已有累计基准不一致。独立路线请从自己的网格输入启动。')
            result['explicit_baseline']={'path':str(baseline_path),'sha256':digest(baseline_path)}
            root_export=root_export if root_export is not None else explicit
        if root_export is not None:
            verify_exports(root_export,exported,require_improvement=False)
        candidate=prepare_edits(exported,max_nodes,passes,reference=root_export)
        if not candidate['edits']: raise RuntimeError('保持边界条件下未找到改善节点。')
        if digest(source)!=initial_hash: raise RuntimeError('源 CAE 已发生变化，停止。')
        apply=folder/'apply'; apply.mkdir(); candidate_path=apply/'model_candidate.cae'; shutil.copy2(source,candidate_path)
        baseline={'warningElements':sorted(exported['warnings']),'failedElements':sorted(exported['errors']),'naElements':sorted(exported['unavailable'])}
        applied=_run_worker(abaqus,apply,'worker_cae_apply.py',{'model_copy':str(candidate_path),'model':model,'part':part,
            'baseline_checks':baseline,'edits':candidate['edits'],'volume_checks':candidate['volume_checks']},
                            recover_completed=True)
        result['worker_returncodes']['apply']=applied.get('returncode')
        result['official_rollback_history']=applied.get('history',[])
        if not applied.get('ok'): raise RuntimeError('CAE 候选官方检查未完成：'+applied.get('error','未知'))
        verify_dir=folder/'verify'; verify_dir.mkdir()
        saved=_run_worker(abaqus,verify_dir,'worker_cae_export.py',{'model_copy':str(candidate_path),'model':model,'part':part},
                          recover_completed=True)
        result['worker_returncodes']['verify']=saved.get('returncode')
        verification=verify_exports(exported,saved)
        cumulative_verification=verify_exports(root_export,saved) if root_export is not None else verification
        result.update({'ok':True,'candidate':str(candidate_path),'candidate_sha256':digest(candidate_path),
            'official_before':len(baseline['warningElements']),'official_after':verification['official_after'],
            'boundary_fixed':candidate['boundary_count'],'moved_nodes':applied['moved_nodes'],
            'independent_before':candidate['independent_before'],
            'independent_after_prior_to_official_rollback':candidate['independent_after'],
            'official_rollback_history':applied['history'], 'saved_candidate_verification':verification,
            'cumulative_verification':cumulative_verification,
            'status':'已完成官方网格质量对照；集合、内部接触与物理结果尚需核对。'})
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        result['error']=str(exc); result['status']='未完成；未发布候选。'
        if isinstance(exc,VolumeGuardError): result['volume_failures']=exc.failures
    result['source_unchanged']=digest(source)==initial_hash
    result['elapsed_s']=round(time.perf_counter()-started,2)
    if not result['source_unchanged']: result['ok']=False;result['candidate']=None;result['status']='源 CAE 在运行期间发生变化，结果作废。'
    result['report']=str(folder/'报告.md')
    (folder/'结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# CAE 内节点网格试验', '', '输入：`%s`；模型：`%s`；部件：`%s`；不启动作业。'%(source,model,part), '',result['status'], '', '原始文件哈希一致：%s。'%result['source_unchanged']]
    if result.get('error'): lines+=['','错误：'+result['error']]
    if result.get('volume_failures'):
        labels=', '.join(str(item['label']) for item in result['volume_failures'][:12])
        lines+=['','体积失败单元示例：'+labels+'。详细值见 `结果.json` 的 `volume_failures`。']
    if result.get('official_rollback_history'):
        lines+=['','各轮官方警告和体积回退详情见 `结果.json` 的 `official_rollback_history`。']
    if result['ok']:
        lines += ['', '官方警告：%s → %s；移动内部节点：%s。' %
                  (result['official_before'], result['official_after'], result['moved_nodes']),
                  '相对首轮网格的最小单元体积比为 %.3f%%，工具下限为 %.0f%%。' %
                  (100*cumulative_verification['min_volume_ratio'], 100*MIN_VOLUME_RATIO),
                  '候选：`%s`。' % result['candidate']]
    lines+=['','限制：']+['- '+s for s in result['limitations']]
    (folder/'报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result
