"""运行相互隔离的 CAE 网格对照，不提交任何作业。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from x1.core import digest, new_run


def candidate_plan():
    """每次仅变更一项；所有方案从同一个基准副本出发。"""
    return [
        {'name':'baseline', 'kind':'baseline'},
        {'name':'size_0p8', 'kind':'size', 'factor':0.8},
        {'name':'size_1p2', 'kind':'size', 'factor':1.2},
        {'name':'mapped_off', 'kind':'mapped_off'},
        {'name':'alternate_tet', 'kind':'alternate'},
    ]


def mesh_study(cae, model, part, abaqus='abaqus', output_dir=None):
    source = Path(cae).resolve()
    if not source.is_file(): raise ValueError('指定 CAE 不存在。')
    if source.suffix.lower() != '.cae': raise ValueError('请输入 CAE 文件。')
    if not model or not part: raise ValueError('请明确模型及部件名称。')
    root = Path(output_dir).resolve() if output_dir else Path(__file__).resolve().parents[1]/'runs'
    if root == source.parent or source.parent in root.parents: raise ValueError('输出目录必须位于源 CAE 目录之外。')
    source_hash = digest(source); folder = new_run(root, 'mesh_study')
    worker = Path(__file__).parent/'worker_mesh_study.py'
    plan = candidate_plan(); outcomes=[]
    for trial in plan:
        trial_dir = folder/trial['name']; trial_dir.mkdir()
        copy = trial_dir/'model_working.cae'; shutil.copy2(source,copy)
        request = {'model_copy':str(copy), 'model':model, 'part':part, 'trial':trial}
        (trial_dir/'request.json').write_text(json.dumps(request,ensure_ascii=True,indent=2),encoding='ascii')
        started=time.perf_counter(); process_code=None
        try:
            process=subprocess.run([abaqus,'cae','noGUI='+str(worker)],cwd=str(trial_dir),
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',timeout=900,
                shell=(os.name=='nt'))
            log=process.stdout; process_code=process.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            log=str(exc)
        (trial_dir/'后台运行日志.txt').write_text(log,encoding='utf-8')
        output=trial_dir/'worker_result.json'
        result=json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'ok':False,'error':log.strip() or '内核未返回结果'}
        result.update({'trial':trial, 'returncode':process_code, 'working_copy':str(copy), 'elapsed_s':round(time.perf_counter()-started,2)})
        if process_code != 0: result['ok']=False
        outcomes.append(result)
        if digest(source)!=source_hash: raise RuntimeError('原始 CAE 在试划过程中发生变化，已停止。')
        if trial['kind']=='baseline' and not result['ok']:
            break
    successful=[x for x in outcomes if x.get('ok')]
    result={'ok':bool(successful) and len(outcomes)==len(plan) and all(x.get('ok') for x in outcomes),
        'version':'X1.4','input':str(source),'input_sha256':source_hash,'source_unchanged':digest(source)==source_hash,
        'model':model,'part':part,'job':'未启动作业','run_dir':str(folder),'trials':outcomes,
        'status':'需复核：仅完成网格检查；接触几何、集合、物理响应和作业数据检查尚需验证。',
        'limitation':'局部边种子会保留；未实施分区、虚拟拓扑、局部节点优化或自动修复；未知的区域依赖不视作通过。'}
    (folder/'结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    text=['# X1.4 网格对照','',f'源 CAE：`{source}`；模型：`{model}`；部件：`{part}`；作业：未启动。','',
          '|方案|状态|错误|警告|未检查|最小稳定增量估计|单元数|','|---|---:|---:|---:|---:|---:|---:|']
    for x in outcomes:
        a=x.get('after',{}); q=a.get('quality',{}); time_info=a.get('stable_time',{})
        text.append('|%s|%s|%s|%s|%s|%s|%s|'%(x['trial']['name'],'完成' if x.get('ok') else '失败',
           q.get('errors','未知'),q.get('warnings','未知'),q.get('unavailable','未知'),time_info.get('worst','未知'),a.get('elements','未知')))
    text += ['',result['status'],result['limitation'], '原文件哈希一致：%s。'%result['source_unchanged'],
             '任何候选副本均不自动替换原工程；须复核区域、接触和结果。']
    (folder/'报告.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    result['report']=str(folder/'报告.md')
    return result
