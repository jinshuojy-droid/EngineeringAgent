"""EngineeringAgent V3.1：多工况同时间对照与故障诊断。"""
import argparse
import csv
import difflib
import hashlib
import json
import math
from pathlib import Path

import log_analysis
from history_analysis import read_history, energy_ratios
from result_mode import PROJECT, new_output, within
from v3_geometry import assess_element
from v3_inp import parse_inp, locate_element
from v3_links import link_element
from v3_tracks import element_track
from v31_support import mesh_structure, estimate, plot_local


def nearest(rows, t, tolerance):
    valid=[r for r in rows if r.get('step_time') is not None and math.isfinite(r['step_time'])]
    if not valid:return None
    lo,hi=min(r['step_time'] for r in valid),max(r['step_time'] for r in valid)
    if t<lo-1e-6 or t>hi+1e-6:return None
    row=min(valid,key=lambda r:abs(r['step_time']-t))
    return row if abs(row['step_time']-t)<=tolerance else None


def load_case(case, step, step_index, times, tolerance, ratio_threshold):
    directory=Path(case['directory']).resolve();target=Path(case['target']).resolve()
    inp=directory/(case['job']+'.inp');odb=directory/(case['job']+'.odb')
    meta=json.loads((target/'target_manifest.json').read_text(encoding='utf-8'))
    if Path(meta['odb_path']).resolve()!=odb.resolve():raise ValueError('局部提取来源与工况 ODB 不一致：'+str(odb))
    if (meta['instance'].upper(),int(meta['element']))!=(case['instance'].upper(),int(case['element'])):
        raise ValueError('局部提取单元与工况设置不一致')
    parsed=parse_inp(inp); link=locate_element(parsed,case['instance'],case['element'])
    track=element_track(target,case['instance'],case['element'],assess_element,str(odb))
    frames=[r for r in track['frames'] if r['step']==step]
    valid_frames=[r for r in frames if r['step_time'] is not None and r['geometry'].get('status') in ('ok','nonpositive_jacobian')]
    logs=log_analysis.diagnose(directory,case['job'])
    records=[r for r in logs['stable_increment']['records'] if r['step']==step_index]
    history=read_history(target/'energy_history.csv')
    regions=sorted({region for st,region,var in history if st==step and var=='ALLAE' and (st,region,'ALLIE') in history})
    region=regions[0] if len(regions)==1 else None
    rows=history.get((step,region,'ALLAE'),[])+history.get((step,region,'ALLIE'),[]) if region else []
    pairs=energy_ratios(history,step,region)['samples'] if region and not any(r['segment'] or r.get('domain','').upper()!='TIME' for r in rows) else []
    ratios=[{'step_time':p['allae']['step_time'],'ratio':p['ratio'],'allae':p['allae']['value'],
             'allie':p['allie']['value'],'ae_line':p['allae']['csv_line'],'ie_line':p['allie']['csv_line']} for p in pairs]
    samples=[]
    for t in times:
        f=nearest(frames,t,tolerance);r=nearest(records,t,tolerance);e=nearest(ratios,t,tolerance)
        g=(f.get('geometry') or {}) if f else {};deformed=g.get('deformed') or {}
        samples.append({'target':t,'increment':r,'energy':e,'frame_index':f['frame_index'] if f else None,
                        'frame_time':f['step_time'] if f else None,'geometry_status':g.get('status'),
                        'scaled_jacobian':deformed.get('minimum_scaled_jacobian'),
                        'peeq':f['peeq'] if f else [],'displacements':f['displacements'] if f else []})
    bad=next((f for f in frames if f['geometry'].get('status')=='nonpositive_jacobian'),None)
    controls=[{'parameters':b['parameters'],'evidence':b['evidence']} for b in parsed['records']['section_controls']]
    sections=[{'parameters':b['parameters'],'evidence':b['evidence']} for b in parsed['records']['sections']
              if str(b['scope'].get('part','')).casefold()==str(link.get('part','')).casefold()]
    reduced=[]
    for block in parsed['records']['elements']:
        kind=str(block['parameters'].get('type','')).upper();part=block['scope'].get('part')
        if not kind.endswith('R') or not part:continue
        matching=[{'parameters':s['parameters'],'evidence':s['evidence']} for s in parsed['records']['sections']
                  if str(s['scope'].get('part','')).casefold()==str(part).casefold()]
        reduced.append({'part':part,'type':kind,'element_evidence':block['evidence'],'sections':matching})
    mesh=mesh_structure(parsed,link['part']) if link.get('part') else {'status':'unknown'}
    last=ratios[-1] if ratios else None
    return {'name':case['name'],'job':case['job'],'directory':str(directory),'inp':str(inp),'odb':str(odb),
            'inp_sha256':hashlib.sha256(inp.read_bytes()).hexdigest(),'snapshot':meta,
            'structure':link,'mesh':mesh,'surface_links':link_element(parsed,case['instance'],case['element']),
            'section_settings':sections,'section_controls':controls,'reduced_integration_parts':reduced,'samples':samples,
            'last_valid_geometry_time':valid_frames[-1]['step_time'] if valid_frames else None,
            'first_negative':{'step_time':bad['step_time'],'frame_index':bad['frame_index'],
                              'source':next((d.get('source') for d in bad['displacements'] if d.get('source')),None)} if bad else None,
            'energy_region':region,'energy_file':str(target/'energy_history.csv'),'last_energy':last,
            'energy_rule':{'threshold':ratio_threshold,'exceeded':last['ratio']>ratio_threshold} if last else None,
            'log_status':logs['status'],'records':records,'geometry_frames':frames,'energy_series':ratios,
            'track':track,'target_dir':str(target),'warnings':logs['diagnostics']}


def inp_changes(first, second):
    a=Path(first['inp']).read_text(encoding='utf-8',errors='replace').splitlines()
    b=Path(second['inp']).read_text(encoding='utf-8',errors='replace').splitlines();changes=[]
    for op,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes():
        if op=='equal':continue
        old=[{'line':n+1,'text':a[n]} for n in range(i,j) if a[n].strip() and not a[n].lstrip().startswith('**')]
        new=[{'line':n+1,'text':b[n]} for n in range(k,l) if b[n].strip() and not b[n].lstrip().startswith('**')]
        if old or new: changes.append({'before':old,'after':new})
    return {'before':first['name'],'after':second['name'],'changes':changes}


def run(spec, out):
    step=spec['step'];step_index=int(spec['step_index']);times=spec['times'];tol=float(spec.get('tolerance',0.0026));threshold=float(spec.get('energy_ratio_threshold',0.2))
    cases=[load_case(c,step,step_index,times,tol,threshold) for c in spec['cases']]
    diffs=[inp_changes(cases[i-1],cases[i]) for i in range(1,len(cases))]
    def signature(case):
        m=case['snapshot'];positions={n['label']:n['coordinates'] for n in m['nodes']}
        return (m['instance'].upper(),m['element_type'],tuple(m['connectivity']),
                tuple(tuple(round(float(v),6) for v in positions[n]) for n in m['connectivity']))
    geometry_comparable=all(signature(c)==signature(cases[0]) for c in cases[1:])
    output={'schema_version':'3.1','step':step,'times':times,'tolerance':tol,'cases':cases,'input_differences':diffs,
            'geometry_comparable':geometry_comparable,'figures':[]}
    lines=['# EngineeringAgent V3.1 多工况诊断','',f'分析步：{step}；对照作业数：{len(cases)}。',
           '对照目标：跟踪关键单元几何、稳定增量和人工能量，找出优先验证方向。','',
           '## 一、核心结论']
    for case in cases:
        bad=case['first_negative']; last=case['last_energy'];lastrec=case['records'][-1] if case['records'] else None
        lines.append('- **%s**：%s；最新能量比 %s；%s。' % (case['name'],
            ('首个采样负雅可比帧 %.6g s（帧 %s；%s:%s 等节点数据）' % (bad['step_time'],bad['frame_index'],bad['source']['file'],bad['source']['line'])) if bad and bad['source'] else ('截至 %.6g s 的可解析保存帧未出现负雅可比'%case['last_valid_geometry_time'] if case['last_valid_geometry_time'] is not None else '无可用几何帧'),
            ('%.1f%%（步时间 %.6g s；%s:%s、%s）' % (100*last['ratio'],last['step_time'],case['energy_file'],last['ae_line'],last['ie_line'])) if last else '未获得',
            ('最新增量 %.6g s（步时间 %.6g；%s:%s）' % (lastrec['stable_increment'],lastrec['step_time'],lastrec['evidence']['file'],lastrec['evidence']['line'])) if lastrec else '缺少增量记录'))
    lines += ['', '## 二、关键时刻对照', '', '| 工况 | 目标时间 s | 增量 s（实际时间） | 几何指标（实际时间） | 单元 PEEQ | ALLAE / ALLIE（实际时间） |', '|---|---:|---|---|---|---|']
    for case in cases:
        for s in case['samples']:
            r,e=s['increment'],s['energy'];g=s['scaled_jacobian']
            peeq=[v['values'][0] for v in s['peeq'] if v.get('values') and v['values'][0] is not None]
            lines.append('| %s | %.5g | %s | %s | %s | %s |' % (case['name'],s['target'],
                ('%.4g (%.5g)'%(r['stable_increment'],r['step_time'])) if r else '无记录',
                ('%.4g (%.5g)'%(g,s['frame_time'])) if g is not None else '无记录',
                ('%.4g'%max(peeq)) if peeq else '无记录',
                ('%.4g / %.4g = %.2f%% (%.5g)'%(e['allae'],e['allie'],100*e['ratio'],e['step_time'])) if e else '无记录'))
    lines += ['', '## 三、结构及设置']
    lines.append('- 目标单元初始连接和坐标：'+('各轮一致。' if geometry_comparable else '存在差异；需空间匹配后再比较同一物理区域。'))
    for case in cases:
        mesh=case['mesh']; sections=case['section_settings']
        if mesh['status']=='structured': meshtext='Part 局部 X=%s、Y=%s、Z=%s 层，共 %s 单元' % (mesh['X'],mesh['Y'],mesh['Z'],mesh['elements'])
        else: meshtext=mesh.get('reason','未知')
        lines.append('- %s：实例 %s，单元 %s，零件 %s，材料/截面 %s；网格 %s。来源：%s。' %
                     (case['name'],case['structure']['instance'],case['structure']['element'],case['structure']['part'],
                      ', '.join(str(s['parameters']) for s in sections) or '未知',meshtext,case['structure']['evidence']))
        if case['section_controls']:lines.append('  - 输入文件中的控制定义：'+str(case['section_controls']))
        state=case['snapshot']
        lines.append('  - ODB 局部读取前后文件状态%s；输入文件 SHA-256：%s。' %
                     ('未变化' if state['stat_before']==state['stat_after'] else '发生变化，请重新读取核对',case['inp_sha256']))
    lines.append('- 最后一轮减缩积分部件及截面：'+ '; '.join('%s/%s，截面 %s（%s:%s）' %
                 (r['part'],r['type'],','.join(str(s['parameters']) for s in r['sections']) or '未知',
                  r['element_evidence']['file'],r['element_evidence']['line']) for r in cases[-1]['reduced_integration_parts']))
    lines += ['', '## 四、实际修改']
    for diff in diffs:
        lines.append('- %s → %s：%s' % (diff['before'],diff['after'],json.dumps(diff['changes'],ensure_ascii=False)))
    lines += ['', '## 五、建议与验证',
              '- **有直接对照：**逐项对照各轮相同保存时间的几何、能量及增量；如果增强版保持正值并且增量改善，再继续检查后续帧。',
              '- **有间接证据：**高人工能量提示检查减缩积分和沙漏控制；先结合绝对值、趋势及区域输出来确定重点部件。',
              '- **待验证：**接触和网格改动的具体作用，按 INP 差异选择单变量对照；对比接触、位移、能量和网格变化。',
              '', '## 六、局部图与时间预估']
    for case in cases:
        rule=case['energy_rule']
        if rule and rule['exceeded']:
            lines.insert(lines.index('## 六、局部图与时间预估')-1,
                         '- **程序规则：**%s 最新能量比超过本次设置的 %.0f%% 提示线；检查分区域能量与沙漏控制。' % (case['name'],100*rule['threshold']))
    for case in cases:
        fig=plot_local(case['track'],case['target_dir'],step,spec['image_time'],out/('local_%s.png'%len(output['figures'])))
        if fig: output['figures'].append(fig);lines.append('- %s：局部图 %s，步时间 %.6g s；红色为目标单元。' % (case['name'],Path(fig['file']).name,fig['step_time']))
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
        for key,title,ylabel in [('records','稳定时间增量对照','稳定增量 (s)'),('geometry_frames','目标单元几何对照','最小采样缩放雅可比'),('energy_series','人工能量比例对照','ALLAE / ALLIE')]:
            fig,ax=plt.subplots();found=False
            for case in cases:
                points=[]
                for row in case[key]:
                    value=(row.get('stable_increment') if key=='records' else ((row['geometry'].get('deformed') or {}).get('minimum_scaled_jacobian') if key=='geometry_frames' else row.get('ratio')))
                    if row.get('step_time') is not None and value is not None and math.isfinite(value):points.append((row['step_time'],value))
                if points:
                    ax.plot([x[0] for x in points],[x[1] for x in points],label=case['name']);found=True
            if found:
                ax.set_title(title);ax.set_xlabel('分析步时间 (s)');ax.set_ylabel(ylabel);ax.legend();ax.grid(True);fig.tight_layout()
                name='对照_'+key+'.png';fig.savefig(out/name,dpi=150);output['figures'].append({'file':str(out/name),'type':key});lines.append('- '+title+'：'+name)
            plt.close(fig)
    except ImportError:
        lines.append('- 未安装 Matplotlib；安装后重新生成对照曲线。')
    latest=cases[-1]
    status=latest['log_status']['value']
    eta=({'status':'not_running','reason':'日志显示作业已结束'} if any(term in status for term in ('完成','终止','失败','中断'))
         else estimate(Path(latest['directory'])/(latest['job']+'.sta'),step_index,float(spec['target_end'])))
    output['latest_eta']=eta
    if eta['status']=='estimate':lines.append('- %s：按近期速度到分析步上限 %.4g s 约 %.1f 分钟；提前满足停止条件将提前结束。来源：%s:%s、%s。' % (latest['name'],eta['target_time'],eta['remaining_wall_seconds']/60,eta['source_file'],eta['first_line'],eta['last_line']))
    lines += ['', '## 七、结果复核',
              '- 几何指标为保存帧的角点采样，坐标方向需与 U 一致；在 CAE 对应帧核对局部形状。',
              '- 各工况的场、日志和能量按实际采样时间展示；核对时间和 ODB 版本后再计算改善幅度。',
              '- 正在运行的 ODB 提取不是原子快照；再次读取最新帧并检查来源文件变化。',
              '- UAMP 子程序、ODB 的来源及运行过程未由 INP 文本差异单独确认；比较前核对运行命令和归档记录。']
    (out/'对照报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (out/'对照数据.json').write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    with (out/'同时间对照.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['工况','目标时间','增量时间','稳定增量','帧时间','帧号','最小缩放雅可比','能量时间','ALLAE','ALLIE','ALLAE_ALLIE'])
        for case in cases:
            for s in case['samples']:
                r=s['increment'] or {};e=s['energy'] or {};w.writerow([case['name'],s['target'],r.get('step_time'),r.get('stable_increment'),s['frame_time'],s['frame_index'],s['scaled_jacobian'],e.get('step_time'),e.get('allae'),e.get('allie'),e.get('ratio')])
    return output


def main(argv=None):
    p=argparse.ArgumentParser(description='使用已有局部提取进行多轮诊断');p.add_argument('--config',required=True);p.add_argument('--output',default=str(PROJECT/'reports'/'v3_1'))
    a=p.parse_args(argv);root=Path(a.output).resolve()
    if not within(root,PROJECT) or within(root,(PROJECT/'versions').resolve()):raise ValueError('输出必须在项目内')
    spec=json.loads(Path(a.config).read_text(encoding='utf-8'))
    if len(spec['cases'])<2:raise ValueError('至少需要两个工况')
    for case in spec['cases']:
        if within(root,Path(case['directory']).resolve()):raise ValueError('输出不能位于源作业目录')
    out=new_output(root)
    for case in spec['cases']:
        if not case.get('target'):
            from v31_target import extract_target
            source=Path(case['directory'])/(case['job']+'.odb')
            case['target']=str(extract_target(source,case['instance'],int(case['element']),out/'局部提取',spec.get('abaqus')))
    run(spec,out);print('EngineeringAgent V3.1 报告：'+str(out));return 0
