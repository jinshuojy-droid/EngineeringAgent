"""统一结果检查：日志、只读 ODB、故障证据与结果交付。"""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import log_analysis
import v33_assessment
import v34_diagnosis
import v35_support
import v36_diagnosis
from v3_inp import parse_inp

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent


def fingerprints(paths):
    records={}
    for path in paths:
        if not path.exists():continue
        digest=hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
        st=path.stat()
        records[str(path)]={'sha256':digest.hexdigest(),'size':st.st_size,'mtime_ns':st.st_mtime_ns}
    return records


def input_index(path):
    """提取输入文件中的单元、截面和控制记录；保留每条源行。"""
    if not path.is_file():return None
    parsed=parse_inp(path)
    result={'input_file':str(path),'complete':parsed['complete'],'files':parsed['files'],
            'warnings':parsed['warnings'],'unsupported_keywords':parsed['unsupported_keywords']}
    for category in ('elements','sections','section_controls','boundary_conditions','loads','contacts','constraints','outputs'):
        result[category]=[{'keyword':x['keyword'],'parameters':x['parameters'],'scope':x['scope'],'evidence':x['evidence'],
                           'data':x['data'] if category in ('boundary_conditions','loads','constraints','contacts','outputs') else []}
                          for x in parsed['records'].get(category,[])]
    return result


def stop_evidence(directory, job, status):
    """区分 VUAMP 结束步与求解器终态；不将锁文件视为运行证据。"""
    result = {'state': status, 'vuamp': None, 'control': None}
    sta = directory / (job + '.sta')
    log = directory / (job + '.log')
    if sta.exists():
        lines, _ = log_analysis.read_lines(sta)
        for no, line in enumerate(lines, 1):
            if 'halted as requested in VUAMP' in line or 'Filter/VUAMP Halt' in line:
                result['vuamp'] = {'file': str(sta), 'line': no, 'text': line.strip()}
                break
    if log.exists():
        lines, _ = log_analysis.read_lines(log)
        for i,line in enumerate(lines):
            if 'CONTROL_STOP' not in line: continue
            parts=line.split()
            try:
                step=int(parts[1]); t=float(parts[2]); force=float(parts[3])
                for following in lines[i+1:i+15]:
                    if 'CONTROL_STOP' in following: continue
                    nums=following.split()
                    if len(nums) >= 3:
                        u,base,delta=map(float,nums[:3])
                        result['control']={'step':step,'step_time':t,'contact_force':force,'u3':u,
                                           'baseline':base,'delta':delta,'file':str(log),'line':i+1,
                                           'values_line':lines.index(following,i+1)+1}
                        break
            except (ValueError,IndexError):
                pass
            break
    return result


def run_subprocess(cmd, cwd, logfile, timeout):
    env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
    try:
        p=subprocess.run(cmd,cwd=str(cwd),capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,timeout=timeout)
        text='command: '+repr(cmd)+'\nexit: '+str(p.returncode)+'\nstdout:\n'+p.stdout+'\nstderr:\n'+p.stderr
        ok=p.returncode==0
    except subprocess.TimeoutExpired:
        text='command: '+repr(cmd)+'\n处理超时，未得到完整数据。';ok=False
    logfile.write_text(text,encoding='utf-8')
    return ok


def make_plots(csv_path, dest, config):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    groups={}
    with csv_path.open(encoding='utf-8',newline='') as f:
        for line,row in enumerate(csv.DictReader(f),2):
            try: val=float(row['value']);t=float(row['step_time'])
            except (ValueError,TypeError): continue
            groups.setdefault((row['step'],row['region'],row['variable']),[]).append((t,val,line))
    charts=[]
    for step in dict.fromkeys(k[0] for k in groups):
        energy=[(k,v) for k,v in groups.items() if k[0]==step and k[2] in ('ALLAE','ALLIE','ALLKE','ALLWK','ETOTAL','ALLFD','ALLVD') and k[1].upper()=='ASSEMBLY ASSEMBLY']
        if energy:
            fig,ax=plt.subplots()
            for key,rows in energy: ax.plot([r[0] for r in rows],[r[1] for r in rows],label=key[2]+' / '+key[1])
            ax.set_title(step+' energy');ax.set_xlabel('Step time');ax.set_ylabel('Energy');ax.legend(fontsize='small');fig.tight_layout()
            path=dest/('energy_'+str(len(charts))+'.png');fig.savefig(path,dpi=150);plt.close(fig);charts.append(str(path))
        requested=config.get('history_region')
        force=[(k,v) for k,v in groups.items() if k[0]==step and k[2].upper().startswith('CFNM') and (not config.get('contact_region') or k[1]==config['contact_region'])]
        variables=[config['displacement_variable']] if config.get('displacement_variable') else ['U1','U3']
        for variable in variables:
            u=[(k,v) for k,v in groups.items() if k[0]==step and k[2]==variable and (not requested or k[1]==requested)]
            if len(u)!=1:continue
            fig,ax=plt.subplots();rows=u[0][1];var=u[0][0][2];ax.plot([r[0] for r in rows],[r[1] for r in rows],color='tab:blue',label=var+' '+u[0][0][1]);ax.set_xlabel('Step time');ax.set_ylabel(var,color='tab:blue')
            if len(force)==1:
                ax2=ax.twinx();rows=force[0][1];ax2.plot([r[0] for r in rows],[r[1] for r in rows],color='tab:red',label='CFNM');ax2.set_ylabel('CFNM',color='tab:red')
            trigger=config.get('_trigger')
            if trigger and trigger.get('step')==config.get('_step_numbers',{}).get(step):ax.axvline(trigger['step_time'],color='black',linestyle='--',label='VUAMP')
            ax.set_title(step+' '+var+(' / CFNM' if len(force)==1 else ''))
            if len(force)==1:
                h1,l1=ax.get_legend_handles_labels();h2,l2=ax2.get_legend_handles_labels();ax.legend(h1+h2,l1+l2,fontsize='small')
            else:ax.legend(fontsize='small')
            fig.tight_layout()
            path=dest/('motion_'+var+'_'+str(len(charts))+'.png');fig.savefig(path,dpi=150);plt.close(fig);charts.append(str(path))
        if force:
            fig,ax=plt.subplots()
            for key,rows in force:ax.plot([r[0] for r in rows],[r[1] for r in rows],label=key[1])
            ax.set_title(step+' CFNM');ax.set_xlabel('Step time');ax.set_ylabel('CFNM');ax.legend(fontsize='small');fig.tight_layout()
            path=dest/('contact_force_'+str(len(charts))+'.png');fig.savefig(path,dpi=150);plt.close(fig);charts.append(str(path))
        area=[(k,v) for k,v in groups.items() if k[0]==step and k[2]=='CAREA']
        if area:
            fig,ax=plt.subplots()
            for key,rows in area:ax.plot([r[0] for r in rows],[r[1] for r in rows],label=key[1])
            ax.set_title(step+' contact area (CAREA)');ax.set_xlabel('Step time');ax.set_ylabel('Area');ax.legend(fontsize='small');fig.tight_layout()
            path=dest/('contact_area_'+str(len(charts))+'.png');fig.savefig(path,dpi=150);plt.close(fig);charts.append(str(path))
    return charts


def energy_ratios(csv_path):
    samples={}
    with csv_path.open(encoding='utf-8',newline='') as f:
        for line,row in enumerate(csv.DictReader(f),2):
            if row['variable'] not in ('ALLAE','ALLIE') or 'ASSEMBLY' not in row['region'].upper():continue
            key=(row['step'],row['region'],row['step_time'])
            try:samples.setdefault(key,{}).setdefault(row['variable'],[]).append((float(row['value']),line))
            except ValueError:continue
    paired=[]
    for (step,region,time),v in samples.items():
        if len(v.get('ALLAE',[]))==1 and len(v.get('ALLIE',[]))==1 and v['ALLIE'][0][0]!=0:
            ae,ie=v['ALLAE'][0],v['ALLIE'][0]
            paired.append({'step':step,'region':region,'time':float(time),'ratio':ae[0]/ie[0],
                           'allae':ae[0],'allie':ie[0],'allae_line':ae[1],'allie_line':ie[1]})
    return [max((x for x in paired if x['step']==step),key=lambda x:x['time']) for step in dict.fromkeys(x['step'] for x in paired)]


def focus_plot(summary, dest):
    focus=summary.get('focus',[])
    negatives=[x for x in focus if x.get('minimum_scaled_jacobian') is not None and x['minimum_scaled_jacobian']<=0]
    if not focus:return None
    end=negatives[0] if negatives else focus[-1]
    start=next((x for x in focus if x['step']==end['step']),focus[0])
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    edges=((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7))
    allpoints=start['coordinates']+end['coordinates']
    limits=[(min(p[i] for p in allpoints),max(p[i] for p in allpoints)) for i in range(3)]
    fig=plt.figure(figsize=(10,5))
    for idx,point in enumerate((start,end),1):
        ax=fig.add_subplot(1,2,idx,projection='3d')
        for a,b in edges:
            ax.plot([point['coordinates'][a][0],point['coordinates'][b][0]],
                    [point['coordinates'][a][1],point['coordinates'][b][1]],
                    [point['coordinates'][a][2],point['coordinates'][b][2]],color='tab:red')
        for setter,(low,high) in zip((ax.set_xlim,ax.set_ylim,ax.set_zlim),limits):
            pad=max((high-low)*0.1,1e-5);setter(low-pad,high+pad)
        ax.set_title('%s frame %s t=%.6g' % (point['step'],point['frame'],point['time']))
        ax.set_xlabel('X');ax.set_ylabel('Y');ax.set_zlabel('Z')
    fig.suptitle('%s element %s; deformation scale 1' % (end['instance'],end['element']))
    fig.tight_layout();path=dest/'target_mesh.png';fig.savefig(path,dpi=150);plt.close(fig)
    return str(path)


def peeq_plot(summary, dest, kind='focus'):
    """对指定单元画已保存帧内积分点最大 PEEQ，不推断未保存峰值。"""
    rows=summary.get('peeq_'+kind,[])
    if not rows:return None
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots()
    for step in dict.fromkeys(r['step'] for r in rows):
        values=[r for r in rows if r['step']==step]
        ax.plot([r['time'] for r in values],[r['value'] for r in values],label=step)
    ax.set_title('PEEQ: %s'%('model maximum per frame' if kind=='global' else '%s / element %s'%(rows[0]['instance'],rows[0]['element'])))
    ax.set_xlabel('Step time');ax.set_ylabel('PEEQ (saved frames)');ax.legend();fig.tight_layout()
    path=dest/('peeq_'+kind+'.png');fig.savefig(path,dpi=150);plt.close(fig)
    return str(path)


def explain_diagnostic(message):
    """把已识别的原始求解器诊断翻译为可验证的中文提示。"""
    upper=message.upper()
    if 'PROCESS TERMINATED BY EXTERNAL REQUEST' in upper:
        return '日志记录作业收到外部终止请求。'
    if 'HISTORY CANNOT BE USED' in upper and 'NUMBER INTERVAL' in upper:
        return '输入文件的历程输出使用了不兼容的 NUMBER INTERVAL 参数。检查对应输出请求，改用适用的时间间隔或频率后先做数据检查。'
    if 'UNKNOWN ASSEMBLY SET' in upper:
        return '输入文件引用了不存在的装配集合。检查输出请求中的集合名称和作用域，再进行数据检查。'
    if 'PARAMETER INTERVAL IS REQUIRED' in upper:
        return '输入文件缺少求解器要求的 INTERVAL 参数。检查相应输出请求的时间间隔设置，再进行数据检查。'
    if 'PENETRATED' in upper:
        return '接触节点出现深穿透，建议检查日志所列节点集和接触面。'
    if 'WARPING' in upper:
        return '接触面出现较大扭曲，建议查看该接触面及下层单元变形。'
    if 'ELEMENTS ARE DISTORTED' in upper:
        return '数据检查发现畸变单元，建议查看 WarnElemDistorted 单元集。'
    return message[:250]


def create_report(out, data):
    logs=data['logs'];state=data['stop'];summary=data.get('odb_summary') or {};results=data.get('contours',[])
    last=logs.get('stable_increment',{}).get('last_recorded')
    status=state['state'] + ('；VUAMP 请求结束分析步' if state['vuamp'] else '')
    meshes=summary.get('mesh',[]);last_mesh=meshes[-1] if meshes else None
    ratios=data.get('energy_ratios',[]);last_energy=ratios[-1] if ratios else None
    mesh_summary=('末个扫描帧检查 %s 个单元，%s 个采样非正'%(last_mesh['checked'],last_mesh['negative_count'])) if last_mesh else '网格未扫描'
    energy_summary=('；%s 人工能量/内能 %.2f%%'%(last_energy['step'],last_energy['ratio']*100)) if last_energy else '；无可配对的人工能量数据'
    motion_summary='已生成位移历程图，接触曲线以图中标注为准' if any('motion_' in str(x) for x in data.get('plots',[])) else '位移或接触历程图未生成'
    assessment=data['assessment']
    lines=['# EngineeringAgent V3.6 作业检查报告','',f"作业：{data['job']}",f"输入目录：{data['directory']}",f"ODB：{data['odb']}",'',
           '## 核心判定',f"**{assessment['level']}：{assessment['label']}**",f'求解状态：{status}',
           '检查通过仅表示已配置的检查范围通过，不等于实验验证。']
    if data.get('sources'):
        lines.append('实际输入候选：%s；命令记录：%s。%s'%(data['sources'].get('inp'),data['sources'].get('command'),data['sources']['source_note']))
    if data.get('v34',{}).get('history_note'):lines.append(data['v34']['history_note'])
    lines += ['', '## 与六步检查的对应',
              '- 0 终态：看下方求解状态和日志警告；成功结束不等于结果已验证。',
              '- 1 能量：看能量账本、比例、曲线和缺失项；提示线是筛查条件。',
              '- 2 变形动画：请结合关键时段自行观看；此报告不自动判定动画。',
              '- 3 位移：查看实际生成的 U1/U3 历程图；区域不唯一或未输出时需要确认。',
              '- 4 接触力：查看 CFNM 曲线、最小值和相邻样本变化；不能仅凭合力判断全部接触面。',
              '- 5 云图：查看实际生成状态和各字段；未生成或字段未验收不能当作通过。',
              '- 补充单元：核对保存帧、单元类型和跳过数量，检查初始网格警告。']
    risk=[c for c in assessment['checks'] if c['status']=='fail']
    for item in risk:
        lines.append(f"- **异常：{item['summary']}** {item['action']}")
        if item['id']=='energy_ratio':
            e=item['evidence']['max'];lines.append(f"  证据：{e['step']} t={e['time']}，ALLAE={e['allae']:.6g}、ALLIE={e['allie']:.6g}；{item['evidence']['source']}:{e['allae_line']}、{e['allie_line']}。")
            first=item['evidence'].get('first_over_limit')
            if first:lines.append(f"  首个符合筛查条件的超线样本：{first['step']} t={first['time']}，比值 {first['ratio']:.2%}；{item['evidence']['source']}:{first['allae_line']}、{first['allie_line']}。")
            lines.append(f"  规则依据：{item['basis']} 内能下限为该步已配对最大值的 {item['evidence']['min_internal_fraction']:.2%}，已排除低能量样本 {item['evidence']['excluded_low_energy']} 个。")
    findings=data.get('v34',{}).get('findings',[])
    if findings:
        lines+=['','## 优先核查']
        for finding in findings:
            if finding['status']=='observed' and finding['id']!='displacement_observation':continue
            lines.append('- **%s** %s'%(finding['summary'],finding['action']))
            for ev in finding.get('evidence',[]):
                lines.append('  来源：%s:%s'%(ev.get('file','未知'),ev.get('line','未知')))
            if finding.get('basis'):lines.append('  规则依据：'+finding['basis'])
    lines+=['','## 检查项状态']
    labels={'pass':'通过','fail':'异常','unknown':'证据不足'}
    check_names={'extraction':'结果读取','analysis_scope':'分析类型','model_settings':'模型设置','energy_ratio':'人工能量','mesh':'网格','displacement':'参考点位移','contact':'接触','integrity':'文件完整性'}
    for item in assessment['checks']:
        action=(' '+item['action']) if item['status']!='pass' and item.get('action') else ''
        lines.append(f"- {check_names.get(item['id'],item['id'])}：{labels[item['status']]}。{item['summary']}{action}")
        if item['id']=='model_settings' and item.get('evidence'):
            for match in item['evidence'].get('matched',[]):
                ev=match['evidence'];de=match.get('data_evidence')
                detail=f"{ev['file']}:{ev['line']}"+(f"、{de['line']}" if de else '')
                lines.append(f"  - {match['expected']['keyword']}：{detail}。")
    lines+=['','## 数值核查与实验对照清单']
    checks_by_id={item['id']:item for item in assessment['checks']}
    for cid,title in [('mesh','已保存帧网格'),('energy_ratio','人工能量'),('contact','接触状态')]:
        item=checks_by_id.get(cid)
        lines.append('- %s：%s。'%(title,labels.get(item['status'],'证据不足') if item else '证据不足'))
    lines.append('- ETOTAL 步内漂移：%s；核对能量曲线和适用尺度。'%('已记录' if data.get('v35',{}).get('energy_balance',{}).get('steps') else '证据不足'))
    lines.append('- 网格无关性：本次单作业未验证；需要同条件多网格对照。')
    lines.append('- 实验对照：本次未验证；需要导入实验数据并明确对比对象。')
    lines+=['','## 四步检查摘要',f"1. 终态：{status}",
           '2. 网格与能量：'+mesh_summary+energy_summary+'。',
           '3. 位移与接触：'+motion_summary+'；多个同名区域需要明确指定。',
           '4. 云图：'+str(sum(x.get('status')=='ok' for x in results))+' 张；详见云图目录。','',
           '## 终态和停止证据']
    if state['state'] != '求解日志显示完成':
        lines.insert(5,'**诊断数据：作业未确认正常完成，图表来自已有输出帧，用于故障分析。**')
    for item in logs.get('status',{}).get('evidence',[]):lines.append(f"- {item['file']}:{item['line']}：{item['text']}")
    if state['vuamp']:
        e=state['vuamp'];lines.append(f"- VUAMP 请求结束分析步：{e['file']}:{e['line']}。")
    if state['control']:
        c=state['control'];lines.append(f"- 控制记录：Step-{c['step']} t={c['step_time']}，U3={c['u3']}，基准={c['baseline']}，相对变化={c['delta']}，接触力={c['contact_force']}；{c['file']}:{c['line']}、{c['values_line']}。")
        goal=data['config'].get('target_displacement')
        if goal is not None:lines.append(f"- 用户设定目标 {goal}，控制记录相对变化{'达到' if c['delta']>=goal else '未达到'}该目标；单位沿用模型。")
    if last:lines.append(f"- 最后记录增量 {last['stable_increment']}，Step-{last['step']} t={last['step_time']}；{last['evidence']['file']}:{last['evidence']['line']}。")
    for step in summary.get('steps',[]):
        lines.append(f"- 分析步 {step['name']}：{step['procedure']}，计划时间 {step['time_period']}，已保存帧数 {step['frame_count']}；来源 ODB / data/odb_summary.json。")
    lines+=['','## 能量平衡与接触面积']
    balance=data.get('v35',{}).get('energy_balance',{})
    for b in balance.get('steps',[]):
        lines.append(f"- {b['step']} / {b['region']}：ETOTAL 起点 {b['initial']['value']:.6g}，步内最大绝对漂移 {b['max_abs_drift']:.6g}，t={b['worst']['time']}；{b['source']}:{b['initial']['line']}、{b['worst']['line']}。")
    if not balance.get('steps'):lines.append('- '+balance.get('reason','没有能量平衡数据')+'；请补充全模型 ETOTAL 历程。')
    areas=data.get('v35',{}).get('contact_areas',[])
    for area in areas:
        lines.append(f"- {area['step']} / {area['region']}：CAREA 最大已保存样本 {area['max']['value']:.6g}，t={area['max']['time']}；{area['source']}:{area['max']['line']}。")
    if not areas:lines.append('- 没有可用 CAREA 历程；如需面积曲线，请在模型中请求相应接触输出。')
    lines+=v36_diagnosis.report_lines(data)
    lines+=['','## 网格检查']
    for mesh in summary.get('mesh',[]):
        worst=mesh['worst'][0] if mesh['worst'] else None
        lines.append(f"- {mesh['step']} 帧 {mesh['frame']} t={mesh['time']}：支持 {mesh['supported']}、已检查 {mesh['checked']}、缺失数据跳过 {mesh['skipped']}、其他类型 {mesh['unsupported']}；采样非正单元数 {mesh['negative_count']}。")
        if worst:lines.append(f"  最差：{worst['instance']} 单元 {worst['element']}，采样缩放雅可比 {worst['value']:.6g}，初始中心 {worst['initial_centroid']}。来源 ODB 帧及 data/odb_summary.json。")
    if not summary.get('mesh'):lines.append('- 未执行或未取得网格检查数据。')
    for e in data.get('energy_ratios',[]):
        lines.append(f"- {e['step']} t={e['time']}：ALLAE={e['allae']:.6g}、ALLIE={e['allie']:.6g}，比值 {e['ratio']:.2%}；data/history.csv:{e['allae_line']}、{e['allie_line']}。")
    displacement=next((c for c in assessment['checks'] if c['id']=='displacement'),None)
    if displacement and displacement.get('evidence'):
        e=displacement['evidence'];lines.append(f"- {e['step']} / {e['region']} / {e['variable']}：t={e['window_start']:.6g} s 起的 {e['window_seconds']} s 窗口，范围 {e['range']:.6g}；最小值 {e['minimum']['value']:.6g}（t={e['minimum']['time']:.9g}）、最大值 {e['maximum']['value']:.6g}（t={e['maximum']['time']:.9g}），来源 {e['source']}:{e['minimum']['line']}、{e['maximum']['line']}。")
    contact=next((c for c in assessment['checks'] if c['id']=='contact'),None)
    if contact and contact.get('evidence') and 'available_history' in contact['evidence']:
        for c in contact['evidence']['available_history']:
            last=c.get('last') or {}
            lines.append(f"- 接触力 {c['step']} / {c['region']} / {c['variable']}：{c['count']} 个样本，最大采样间隔 {c['max_sample_interval']:.6g}，零值样本 {c['zeros']}；最后样本 {c['last']['value'] if last else '未知'}，{contact['evidence']['history_source']}:{last.get('line','未知')}。")
    focus=summary.get('focus',[])
    negatives=[x for x in focus if x.get('minimum_scaled_jacobian') is not None and x['minimum_scaled_jacobian']<=0]
    if focus:
        if negatives:
            point=negatives[0]
            lines.append(f"- 追踪单元（{data['focus_method']}）{point['instance']} / {point['element']} 首次已保存采样非正：{point['step']} 帧 {point['frame']} t={point['time']}，缩放雅可比 {point['minimum_scaled_jacobian']:.6g}；来源 ODB 及 data/odb_summary.json。")
        else:lines.append(f"- 追踪单元（{data['focus_method']}）{focus[0]['instance']} / {focus[0]['element']} 已保存帧采样未发现非正雅可比；数据见 data/odb_summary.json。")
    peeq=summary.get('peeq_focus',[])
    if peeq:
        peak=max(peeq,key=lambda x:x['value'])
        lines.append(f"- 指定/临界单元 PEEQ 最大保存样本：{peak['instance']} 单元 {peak['element']}，{peak['step']} 帧 {peak['frame']} t={peak['time']}，PEEQ={peak['value']:.6g}，积分点 {peak['integration_point']}；来源 {data['odb']} 及 data/odb_summary.json。")
    global_peeq=summary.get('peeq_global',[])
    if global_peeq:
        peak=max(global_peeq,key=lambda x:x['value'])
        lines.append(f"- 全模型 PEEQ 最大保存样本：{peak['instance']} 单元 {peak['element']}，{peak['step']} 帧 {peak['frame']} t={peak['time']}，PEEQ={peak['value']:.6g}；来源 {data['odb']} 及 data/odb_summary.json。")
    lines+=['','## 图表']
    for figure in data.get('plots',[]):
        path=Path(figure);lines.append(f'- 曲线或局部网格图：[{path.name}](plots/{path.name})')
    for item in results:
        if item.get('status')=='ok':
            name=Path(item['file']).name
            lines.append(f"- [{item['variable']}](contours/{name})：{item['step']} 帧 {item['frame']}，时间 {item['time']}，变形比例 {item['scale_factor']}。")
        else:lines.append(f"- {item['variable']}：{item['status']}，{item.get('error','该帧没有该变量')}。")
    if not data.get('plots') and not results:lines.append('- 没有生成图表，请检查运行记录与 ODB 内容。')
    lines+=['','## 日志异常']
    priority=sorted(logs.get('diagnostics',[]),key=lambda x:(x['severity']!='error',not any(w in x['message'].upper() for w in ('PENETRAT','DISTORT','WARP'))))
    for item in priority[:6]:
        explain=explain_diagnostic(item['message'])
        lines.append(f"- {item['severity']}：{explain}（{item['file']}:{item['line_start']}）")
    if not logs.get('diagnostics'):lines.append('- 未提取到日志错误或警告；检查日志来源和完整性。')
    model=data.get('input_index')
    if model:
        lines+=['','## 输入文件中的单元与控制设置']
        for e in model['elements']:
            typ=str(e['parameters'].get('type','')).upper()
            if not typ.endswith('R'):continue
            ev=e['evidence'];lines.append(f"- 零件 {e['scope'].get('part') or '未知'}：{typ}；{ev['file']}:{ev['line']}。")
        for section in model['sections']:
            ev=section['evidence'];p=section['parameters'];lines.append(f"- 截面 {section['scope'].get('part') or '未知'}：材料 {p.get('material','未知')}，控制 {p.get('controls','未显式指定')}；{ev['file']}:{ev['line']}。")
        for control in model['section_controls']:
            ev=control['evidence'];lines.append(f"- 截面控制 {control['parameters']}；{ev['file']}:{ev['line']}。")
        lines.append('- 上述设置是输入事实；需结合分区域能量与对照计算判断其影响。')
    lines+=['','## 文件完整性']
    integ=data['integrity'];baseline=integ.get('baseline')
    if baseline:
        lines.append(f"- 当前基准登记时间 {baseline['registered_at']}，SHA-256：{baseline['sha256']}；基准来源：{baseline['origin']}。")
        lines.append('- 检查前与基准：'+('一致' if integ['baseline_matches'] else '不一致，立即核查。'))
    else:lines.append('- 未登记该 ODB 的基准；核对来源后可登记当前状态。')
    timing=data.get('late_modification')
    if timing and timing['later_than_log_time']:
        lines.append(f"- 文件修改时间 {timing['odb_mtime']} 晚于日志日期 {timing['log_time']}（{timing['log_file']}:{timing['line']}）；建议核查记录，该时间差本身不说明篡改。")
    lines+=['','## 后续检查','- 已确认的异常以本报告中的数值和日志为准；沙漏、接触或模型响应的具体影响仍需对照验证。',
            '- 优先核对关键警告、接触和能量曲线；必要时在 Abaqus 中对异常帧复核。',
            '- 若出现深穿透或单元扭曲，检查相关接触面、约束及首个异常帧，并采用单变量对照验证。',
            '- 初始坐标与 U 重建的采样结果仅覆盖报告所列帧和单元；在 CAE 中复核对应区域。',
            '- 若变量缺失或可视化失败，检查运行记录与 ODB 输出设置。',
            '', '## 执行说明',f"- 网格模式：{data['scan']}；快速模式只检查各步首末帧。",
            f"- ODB 提取：{data['extract_status']}；云图处理：{data['contour_status']}。",
            '- 输入文件前后哈希：'+('一致' if data.get('inputs_unchanged') else '变化或无法确定，请核对运行记录。')]
    (out/'检查报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None):
    started=time.perf_counter()
    p=argparse.ArgumentParser(description='只读 Abaqus 作业检查与结果交付')
    p.add_argument('--odb');p.add_argument('--directory');p.add_argument('--job')
    p.add_argument('--inp',help='指定实际 INP；默认从作业命令文件识别')
    p.add_argument('--user-subroutine',help='指定用户子程序文件')
    p.add_argument('--history-csv',help='复用已有历史数据；须自行确认与作业对应')
    p.add_argument('--preflight',action='store_true',help='仅检查日志、INP 和可选历史数据，不打开 ODB 或绘图')
    p.add_argument('--output',default=str(Path.home()/'Desktop'/'Abaqus Agent Output'))
    p.add_argument('--config',help='检查配置 JSON');p.add_argument('--abaqus');p.add_argument('--scan',choices=('quick','full','none'),default='quick')
    p.add_argument('--no-contours',action='store_true',help='不启动 CAE 云图导出')
    p.add_argument('--contour-frames',type=int,default=1,help='末分析步选取的云图帧数；默认末帧')
    p.add_argument('--peeq-global',action='store_true',help='逐保存帧提取全模型 PEEQ 峰值')
    p.add_argument('--registry',default=str(PROJECT/'config'/'odb_hash_registry.json'),help='基准登记表（项目内）')
    p.add_argument('--register-baseline',action='store_true',help='首次登记当前 ODB 哈希，不覆盖已有不同基准')
    a=p.parse_args(argv)
    try:
        if a.contour_frames < 1:raise ValueError('云图帧数必须为正')
        if a.odb:
            odb=Path(a.odb).resolve()
            if not odb.is_file() or odb.suffix.lower()!='.odb':raise ValueError('未找到 ODB 文件')
        else:
            if not a.directory or not a.job or Path(a.job).name!=a.job:raise ValueError('需要指定 --odb 或 --directory 与 --job')
            odb=(Path(a.directory)/ (a.job+'.odb')).resolve()
            if not odb.parent.is_dir():raise ValueError('未找到作业目录')
        root=Path(a.output).resolve();source=odb.parent
        if root==source or source in root.parents:raise ValueError('输出目录不能位于原始作业目录')
        sources=v34_diagnosis.resolve_sources(source,odb.stem,a.inp,a.user_subroutine)
        config_match=v35_support.matching_config(PROJECT/'config'/'review_config_registry.json',sources.get('inp')) if not a.config else None
        config_path=a.config or (config_match['path'] if config_match else None)
        config=json.loads(Path(config_path).read_text(encoding='utf-8')) if config_path else {}
        if bool(config.get('instance')) != bool(config.get('element')):raise ValueError('配置的实例与单元必须同时提供')
        root.mkdir(parents=True,exist_ok=True)
        out=root/(odb.stem+'_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]);out.mkdir()
        for part in ('data','plots','contours','logs'):(out/part).mkdir()
        input_paths=([] if a.preflight else [odb])+[source/(odb.stem+ext) for ext in ('.inp','.sta','.msg','.dat','.log','.com')]
        input_paths += [Path(sources[k]) for k in ('inp','user') if sources.get(k)]
        if a.history_csv:input_paths.append(Path(a.history_csv).resolve())
        before=fingerprints(input_paths)
        registry=Path(a.registry).resolve()
        if PROJECT not in registry.parents or registry==source or source in registry.parents:
            raise ValueError('基准登记表必须在项目目录内且不能位于源作业目录')
        if a.register_baseline:
            if str(odb) not in before:raise ValueError('没有 ODB，不能登记基准')
            v33_assessment.register_baseline(registry,odb,before[str(odb)])
        before_integrity=v33_assessment.integrity_check(registry,odb,before,before)
        try:logs=log_analysis.diagnose(source,odb.stem)
        except (OSError,ValueError) as exc:logs={'status':{'value':'未知','evidence':[]},'diagnostics':[],'stable_increment':{},'error':str(exc)}
        stop=stop_evidence(source,odb.stem,logs['status']['value'])
        if sources.get('command'):
            command_text=Path(sources['command']).read_text(encoding='utf-8',errors='replace')
            if re.search(r"['\"]datacheck['\"]\s*:\s*(?:1|ON|True)\b",command_text,re.I):
                stop['state']='当前命令文件为数据检查；完整求解状态需核对同次作业记录'
        model_index=input_index(Path(sources['inp'])) if sources.get('inp') else None
        model_audit=v34_diagnosis.analyze_input(sources.get('inp'),sources.get('user'))
        data={'version':'3.6','odb':str(odb),'directory':str(source),'job':odb.stem,'scan':a.scan,'config':config,'input_index':model_index,
              'logs':logs,'stop':stop,'extract_status':'未执行','contour_status':'未执行','odb_summary':None,'contours':[],'plots':[],'energy_ratios':[],'focus_method':'未指定'}
        data['sources']=sources;data['config_source']={'path':config_path,'registry_match':config_match}
        preflight_history=Path(a.history_csv).resolve() if a.history_csv else None
        data['v34']={'model':model_audit,'findings':model_audit['findings']+v34_diagnosis.history_findings(preflight_history)}
        if a.history_csv:
            data['v34']['history_note']='外部历程由用户指定，需核对与输入是否属于同次求解。'
        dat=source/(odb.stem+'.dat')
        if dat.is_file():
            dat_lines=dat.read_text(encoding='utf-8',errors='replace').splitlines()
            for i,line in enumerate(dat_lines):
                if 'WARNING' not in line.upper():continue
                excerpt=' '.join(dat_lines[i:i+8]).upper()
                if 'DEFINITION=USER' in excerpt and 'TIME' in excerpt and 'IGNORE' in excerpt:
                    data['v34']['findings'].append({'id':'user_amplitude_ignored','status':'warning',
                        'summary':'数据检查报告 USER 幅值中的 TIME 参数被忽略。',
                        'action':'核对原始 DAT 警告及后续步载荷定义，不依赖单独更改 TIME 解决。',
                        'evidence':[{'file':str(dat),'line':i+1}]})
                    break
        data['v34']['findings']+=v34_diagnosis.correlate(data['v34']['findings'])
        data['timing']={'preflight_seconds':time.perf_counter()-started}
        # 先交付轻量检查，再执行可能耗时的 ODB 与绘图。
        (out/'data'/'preflight.json').write_text(json.dumps(data['v34'],ensure_ascii=False,indent=2),encoding='utf-8')
        preflight_lines=['# V3.6 快速检查', '', '作业：'+odb.stem, '输入：'+str(sources.get('inp')), '']
        for finding in data['v34']['findings']:
            preflight_lines.append('- '+finding['summary']+' '+finding['action'])
            for ev in finding.get('evidence',[]):preflight_lines.append('  来源：%s:%s'%(ev.get('file'),ev.get('line')))
        (out/'快速检查.md').write_text('\n'.join(preflight_lines)+'\n',encoding='utf-8')
        print('快速检查目录：'+str(out),flush=True)
        cmd=a.abaqus or shutil.which('abaqus') or shutil.which('abaqus.bat')
        if a.preflight:
            data['extract_status']='快速检查，未读取 ODB';data['contour_status']='快速检查，未生成'
        elif before_integrity['baseline_matches'] is False:
            data['extract_status']='基准不一致，已停止读取 ODB';data['contour_status']='基准不一致，未生成'
        elif not odb.exists():
            data['extract_status']='无 ODB，已执行日志诊断';data['contour_status']='无 ODB，未生成'
        elif cmd:
            extract_cmd=[cmd,'python',str(SCRIPT_DIR/'review_odb.py'),'--odb',str(odb),'--output',str(out/'data'),'--scan',a.scan]
            if a.peeq_global:extract_cmd.append('--peeq-global')
            critical=logs.get('stable_increment',{}).get('minimum_recorded') or {}
            focus_instance=config.get('instance') or critical.get('instance')
            focus_element=config.get('element') or critical.get('element')
            if focus_instance and focus_element:
                data['focus_method']='用户配置' if config.get('instance') and config.get('element') else 'STA 最小记录增量关联单元'
                extract_cmd+=['--focus-instance',str(focus_instance),'--focus-element',str(focus_element)]
            ok=run_subprocess(extract_cmd,out,out/'logs'/'odb_extract.log',900)
            meta=out/'data'/'odb_summary.json'
            if meta.exists():data['odb_summary']=json.loads(meta.read_text(encoding='utf-8'))
            ok=ok and bool((data['odb_summary'] or {}).get('complete'))
            data['extract_status']='成功' if ok else '失败，见运行记录'
            if ok:
                mesh_plot=focus_plot(data['odb_summary'],out/'plots')
                if mesh_plot:data['plots'].append(mesh_plot)
                peeq_chart=peeq_plot(data['odb_summary'],out/'plots')
                if peeq_chart:data['plots'].append(peeq_chart)
                global_chart=peeq_plot(data['odb_summary'],out/'plots','global')
                if global_chart:data['plots'].append(global_chart)
            history=out/'data'/'history.csv'
            if history.exists():
                config['_trigger']=stop['control'];config['_step_numbers']={s['name']:i+1 for i,s in enumerate((data['odb_summary'] or {}).get('steps',[]))}
                data['plots']+=make_plots(history,out/'plots',config)
                data['energy_ratios']=energy_ratios(history)
            if not a.no_contours:
                contour_cmd=[cmd,'cae','noGUI='+str(SCRIPT_DIR/'review_contours.py'),'--','--odb',str(odb),'--output',str(out/'contours'),'--frames',str(a.contour_frames)]
                if config.get('camera'):
                    camera_path=out/'data'/'camera.json'
                    camera_path.write_text(json.dumps(config['camera'],ensure_ascii=False,indent=2),encoding='utf-8')
                    contour_cmd += ['--camera-config',str(camera_path)]
                ok=run_subprocess(contour_cmd,out,out/'logs'/'contours.log',600)
                contour_file=out/'contours'/'contours.json'
                if contour_file.exists():data['contours']=json.loads(contour_file.read_text(encoding='utf-8'))
                ok=ok and bool(data['contours']) and any(c.get('status')=='ok' for c in data['contours'])
                data['contour_status']=('部分生成，部分变量未生成' if ok and any(c.get('status')!='ok' for c in data['contours']) else '成功') if ok else '失败或未生成，见运行记录'
        else:data['extract_status']='未找到 Abaqus 命令';data['contour_status']='未找到 Abaqus 命令'
        after=fingerprints(input_paths)
        data['input_hashes_before']=before;data['input_hashes_after']=after;data['inputs_unchanged']=(before==after)
        data['integrity']=v33_assessment.integrity_check(registry,odb,before,after)
        data['late_modification']=v33_assessment.late_modification(odb,source/(odb.stem+'.log'))
        data['assessment']=v33_assessment.assess(data,out/'data'/'history.csv',config,data['integrity'])
        if not a.history_csv:
            data['v34']['findings']+=v34_diagnosis.history_findings(out/'data'/'history.csv')
            data['v34']['findings']+=v34_diagnosis.correlate(data['v34']['findings'])
        if any(f['status'] in ('warning','unknown') for f in data['v34']['findings']) and data['assessment']['level']=='一级':
            data['assessment']['level']='二级';data['assessment']['label']='完成，模型检查有待核实项目'
        history_path=out/'data'/'history.csv'
        if not history_path.exists() and preflight_history:history_path=preflight_history
        data['v35']={'energy_balance':v35_support.energy_balance(history_path,config.get('energy_drift_threshold'),config.get('energy_drift_scale')),
                     'contact_areas':v35_support.contact_area_history(history_path)}
        data['v36']={'history':v36_diagnosis.review_history(history_path)}
        if data['v35']['energy_balance']['status']=='fail':
            data['assessment']['level']='二级' if data['assessment']['level']=='一级' else data['assessment']['level']
            data['assessment']['label'] += '；ETOTAL 漂移超过配置阈值'
        draft=v35_support.config_draft(model_index,data.get('odb_summary'))
        (out/'data'/'config_draft.json').write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding='utf-8')
        data['timing']['total_seconds']=time.perf_counter()-started
        (out/'data'/'review.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        create_report(out,data)
        print('EngineeringAgent 检查目录：'+str(out))
        return 0 if data['extract_status'] in ('成功','无 ODB，已执行日志诊断','快速检查，未读取 ODB') else 2
    except (OSError,ValueError,KeyError,ImportError,json.JSONDecodeError) as exc:
        print('检查未完成：'+str(exc),file=sys.stderr);return 2
