"""V3.3：证据驱动的结果分级、可配置检查与当前基准登记。"""
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re


DEFAULT_ENERGY_LIMIT = 0.20
DEFAULT_REQUIRED = ('extraction', 'analysis_scope', 'model_settings', 'energy_ratio', 'mesh', 'displacement', 'contact', 'integrity')


def _number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def history_evidence(path, config):
    """提取同一时间的总能量与指定区域短时位移范围，记录 CSV 行号。"""
    samples={}; windows={}; contacts={}
    variable=config.get('displacement_variable','U3')
    wanted_region=config.get('history_region')
    wanted_contact=config.get('contact_region')
    span=_number(config.get('displacement_window_seconds',0.0001))
    if span is None or span<=0:raise ValueError('displacement_window_seconds 必须是正数')
    with Path(path).open(encoding='utf-8',newline='') as f:
        for line,row in enumerate(csv.DictReader(f),2):
            step=row.get('step','');region=row.get('region','');var=row.get('variable','')
            t=_number(row.get('step_time'));value=_number(row.get('value'))
            if t is None or value is None:continue
            if region.upper()=='ASSEMBLY ASSEMBLY' and var in ('ALLAE','ALLIE'):
                samples.setdefault((step,region,row['step_time']),{}).setdefault(var,[]).append((value,line))
            if wanted_region and region==wanted_region and var==variable and (config.get('displacement_steps') is None or step in config['displacement_steps']):
                key=(step,region,var,math.floor((t+span*1e-7)/span))
                w=windows.setdefault(key,{'step':step,'region':region,'variable':var,'window_start':key[-1]*span,
                    'window_seconds':span,'minimum':None,'maximum':None,'count':0,'first_time':t,'last_time':t,'max_sample_interval':0.0})
                point={'value':value,'time':t,'line':line}
                if w['minimum'] is None or value<w['minimum']['value']:w['minimum']=point
                if w['maximum'] is None or value>w['maximum']['value']:w['maximum']=point
                w['max_sample_interval']=max(w['max_sample_interval'],t-w['last_time'])
                w['last_time']=t;w['count']+=1
            if var.upper().startswith('CFNM') and (not wanted_contact or region==wanted_contact):
                k=(step,region,var);c=contacts.setdefault(k,{'step':step,'region':region,'variable':var,'count':0,'zeros':0,'max_sample_interval':0.,'previous':None})
                if c['previous'] is not None:c['max_sample_interval']=max(c['max_sample_interval'],t-c['previous'])
                c['previous']=t;c['count']+=1
                c['last']={'time':t,'value':value,'line':line}
                if value==0:
                    c['zeros']+=1
                    if 'first_zero' not in c:c['first_zero']={'time':t,'line':line}
    energy=[]
    for (step,region,time),vals in samples.items():
        if len(vals.get('ALLAE',[]))!=1 or len(vals.get('ALLIE',[]))!=1:continue
        ae,ie=vals['ALLAE'][0],vals['ALLIE'][0]
        if ie[0]<=0:continue
        energy.append({'step':step,'region':region,'time':float(time),'allae':ae[0],'allie':ie[0],
                       'ratio':ae[0]/ie[0],'allae_line':ae[1],'allie_line':ie[1]})
    displacement=[]
    for w in windows.values():
        if w['count']>=2:
            w['range']=w['maximum']['value']-w['minimum']['value'];displacement.append(w)
    for x in contacts.values():x.pop('previous',None)
    fraction=_number(config.get('energy_min_internal_fraction',0.01))
    if fraction is None or not 0<=fraction<1:raise ValueError('energy_min_internal_fraction 必须在 0 到 1 之间')
    maxima={}
    for row in energy:maxima[row['step']]=max(maxima.get(row['step'],0),row['allie'])
    eligible=[row for row in energy if row['allie']>=maxima[row['step']]*fraction]
    return {'energy':eligible,'energy_excluded':len(energy)-len(eligible),
            'displacement':displacement,'contacts':list(contacts.values())}


def check(identifier,status,summary,evidence=None,action=None,basis=None):
    return {'id':identifier,'status':status,'summary':summary,'evidence':evidence,
            'action':action,'basis':basis}


def assess(data, history_path, config, integrity):
    """输出检查事实与规则，不将未实现检查视作通过。"""
    requested_steps=config.get('displacement_steps')
    if requested_steps is not None and (not isinstance(requested_steps,list) or not requested_steps):raise ValueError('displacement_steps 必须是非空数组')
    summary=data.get('odb_summary') or {};steps=summary.get('steps',[])
    explicit=any('EXPLICIT' in str(s.get('procedure','')).upper() for s in steps)
    history=history_evidence(history_path,config) if history_path and Path(history_path).is_file() else {'energy':[],'energy_excluded':0,'displacement':[],'contacts':[]}
    time_steps={s['name'] for s in steps if str(s.get('domain','')).upper()=='TIME'}
    for key in ('energy','displacement','contacts'):
        history[key]=[row for row in history[key] if row['step'] in time_steps]
    extraction_ok=data.get('extract_status')=='成功' and summary.get('complete') is True
    checks=[check('extraction','pass' if extraction_ok else 'unknown',
                  'ODB 提取已完成。' if extraction_ok else 'ODB 提取不完整或不可用。',
                  {'status':data.get('extract_status')},'检查 ODB、许可及运行记录。')]
    supported_scope=bool(steps) and all('EXPLICIT' in str(s.get('procedure','')).upper() and str(s.get('domain','')).upper()=='TIME' for s in steps)
    checks.append(check('analysis_scope','pass' if supported_scope else 'unknown',
                        '已识别为时间域 Explicit 分析。' if supported_scope else '分析过程包含尚未验收的类型或无法识别。',
                        {'procedures':[s.get('procedure') for s in steps]},'使用对应分析类型的规则和真实算例补充验收。'))
    model=data.get('input_index');expectations=config.get('model_expectations')
    if not model or not model.get('complete') or not expectations:
        checks.append(check('model_settings','unknown','缺少完整 INP 索引或未配置需要核对的模型设置。',
                            action='按工程需求配置边界、载荷或约束预期，并核对 INP/INCLUDE 完整性。'))
    else:
        if not isinstance(expectations,list):raise ValueError('model_expectations 必须是数组')
        matched=[];missing=[];unverifiable=[]
        allowed={'boundary_conditions','loads','constraints','contacts'}
        for expected in expectations:
            if not isinstance(expected,dict) or expected.get('category') not in allowed or not expected.get('keyword') or not (expected.get('parameters') or expected.get('data_prefix')):
                unverifiable.append(expected);continue
            candidates=model.get(expected['category'],[])
            def same(actual,wanted):return str(actual or '').casefold()==str(wanted or '').casefold()
            def match(record):
                if not same(record['keyword'],expected['keyword']):return False
                if any(not same(record['parameters'].get(k),v) for k,v in expected.get('parameters',{}).items()):return False
                if any(not same(record['scope'].get(k),v) for k,v in expected.get('scope',{}).items()):return False
                prefix=expected.get('data_prefix')
                if prefix and not any(''.join(x['text'].split()).casefold().startswith(''.join(prefix.split()).casefold()) for x in record.get('data',[])):return False
                return True
            found=next((r for r in candidates if match(r)),None)
            if found:
                prefix=expected.get('data_prefix')
                data_match=next((x for x in found.get('data',[]) if prefix and ''.join(x['text'].split()).casefold().startswith(''.join(prefix.split()).casefold())),None)
                matched.append({'expected':expected,'evidence':found['evidence'],
                                'data_evidence':data_match.get('evidence') if data_match else None})
            else:missing.append(expected)
        status='fail' if missing else ('unknown' if unverifiable else 'pass')
        checks.append(check('model_settings',status,'仅核对配置的输入记录：匹配 %s，未匹配 %s，不可核对 %s；尚未自动排查约束冲突。'%(len(matched),len(missing),len(unverifiable)),
                            {'matched':matched,'missing':missing,'unverifiable':unverifiable},
                            '核对未匹配的边界、载荷或约束，并检查传递与相互作用。',
                            '只核对明确配置的 INP 记录，不证明约束互不冲突或真实物理成立。'))
    limit=_number(config.get('energy_ratio_threshold',DEFAULT_ENERGY_LIMIT))
    if limit is None or limit<0:raise ValueError('energy_ratio_threshold 必须是非负数')
    if not explicit:
        checks.append(check('energy_ratio','unknown','非 Explicit 分析或过程未知：未自动套用显式能量提示线。',action='按分析类型配置适用的能量检查。'))
    elif not history['energy']:
        checks.append(check('energy_ratio','unknown','没有可唯一配对的全模型 ALLAE 和正 ALLIE 数据。',action='核对能量历程输出并重新检查。'))
    else:
        highest=max(history['energy'],key=lambda x:x['ratio'])
        last=max(history['energy'],key=lambda x:(next((i for i,s in enumerate(steps) if s['name']==x['step']),-1),x['time']))
        over=[x for x in history['energy'] if x['ratio']>limit]
        first=over[0] if over else None
        expected={s['name'] for s in steps if 'EXPLICIT' in str(s.get('procedure','')).upper() and str(s.get('domain','')).upper()=='TIME'}
        missing_steps=sorted(expected-{e['step'] for e in history['energy']})
        evidence={'source':str(history_path),'max':highest,'last':last,'first_over_limit':first,'matched_samples':len(history['energy']),
                  'excluded_low_energy':history['energy_excluded'],'min_internal_fraction':config.get('energy_min_internal_fraction',0.01),
                  'missing_steps':missing_steps}
        energy_status='fail' if over else ('unknown' if missing_steps else 'pass')
        energy_summary='已保存样本最高 ALLAE/ALLIE %.2f%%，提示线 %.2f%%。'%(highest['ratio']*100,limit*100)
        if missing_steps:energy_summary+=' 以下分析步缺少可用能量对：'+', '.join(missing_steps)+'。'
        checks.append(check('energy_ratio',energy_status,energy_summary,evidence,
            '核对能量绝对值、曲线、减缩积分和沙漏设置；有分区域数据时定位能量来源。',
            '可配置筛查提示线；剔除内能接近零的样本，单独超线不能确定原因。'))
    mesh=summary.get('mesh',[])
    bad=[m for m in mesh if m.get('negative_count',0)>0]
    incomplete=(not mesh or data.get('scan')!='full' or not steps or
                len(mesh)!=sum(s.get('frame_count',0) for s in steps) or
                any(m.get('checked',0)==0 or m.get('skipped',0)>0 or m.get('unsupported',0)>0 for m in mesh) or
                not config.get('coordinate_system_confirmed',False))
    if bad:
        m=bad[0]
        checks.append(check('mesh','fail','已保存帧发现采样雅可比非正单元。',
            {'odb':data.get('odb'),'step':m['step'],'frame':m['frame'],'time':m['time'],'count':m['negative_count'],'worst':m.get('worst',[])[:1]},
            '在 CAE 中定位对应实例与单元，检查该帧附近接触、约束和网格。'))
    elif incomplete:
        checks.append(check('mesh','unknown','网格扫描覆盖不足或坐标系未确认。',
            {'scan':data.get('scan'),'frames_scanned':len(mesh),'frames_available':sum(s.get('frame_count',0) for s in steps)},
            '使用全帧扫描，检查未支持单元，并确认位移与节点坐标系一致。'))
    else:checks.append(check('mesh','pass','配置范围内已保存帧的受支持单元采样雅可比均为正。',{'frames_scanned':len(mesh)},'抽查关键区域。'))
    if not config.get('history_region'):
        checks.append(check('displacement','unknown','未指定待检查位移的历程区域，未猜测参考点。',action='配置 history_region 与 displacement_variable。'))
    elif not history['displacement']:
        checks.append(check('displacement','unknown','指定区域没有足够的位移采样。',action='核对历程变量及输出频率。'))
    else:
        peak=max(history['displacement'],key=lambda x:x['range'])
        threshold=_number(config.get('displacement_range_threshold'))
        if threshold is not None and threshold<0:raise ValueError('displacement_range_threshold 不能为负')
        expected=set(requested_steps) if requested_steps else time_steps
        missing_steps=sorted(expected-{w['step'] for w in history['displacement']})
        status=('fail' if threshold is not None and peak['range']>threshold else
                ('unknown' if threshold is None or missing_steps else 'pass'))
        text='最大已保存窗口范围 %.6g，窗口 %.6g s。'%(peak['range'],peak['window_seconds'])
        if missing_steps:text+=' 以下分析步缺少位移数据：'+', '.join(missing_steps)+'。'
        checks.append(check('displacement',status,text,
            {'source':str(history_path),**peak,'missing_steps':missing_steps},'核对方向、采样和波动成因；根据设备要求配置筛查阈值。',
            '窗口范围包含该窗口内的慢变趋势，不等同单周期振幅。'))
    control=data.get('stop',{}).get('control')
    if control is not None and control.get('contact_force')==0:
        checks.append(check('contact','unknown','停止记录中接触力传感器值为零，需核查其定义与接触状态。',
            {'stop':control,'available_history':history['contacts'],'history_source':str(history_path)},
            '核对接触面、CFNM、CPRESS 和采样；必要时提高历程输出频率。',
            '零合力或稀疏采样本身不能证明实际接触失效。'))
    else:
        checks.append(check('contact','unknown','接触状态尚未经过针对当前模型的完整检查。',
            {'available_history':history['contacts'],'history_source':str(history_path)},'确认接触面和相关输出，再配置判断条件。'))
    if integrity.get('baseline_matches') is False or integrity.get('unchanged_during_review') is False:
        checks.append(check('integrity','fail','输入文件与登记基准不一致或检查期间发生变化。',integrity,'停止依赖该次检查结果，核对变更原因及备份。'))
    elif integrity.get('registered') is False:
        checks.append(check('integrity','unknown','没有已登记的 ODB 基准哈希。',integrity, '核对来源后登记当前状态作为新基准。'))
    elif integrity.get('baseline_matches') and integrity.get('unchanged_during_review'):
        checks.append(check('integrity','pass','ODB 与登记基准一致，本次输入文件哈希未变。',integrity,'保留本次检查记录。'))
    else:checks.append(check('integrity','unknown','文件完整性证据不足。',integrity,'检查哈希记录和文件读写状态。'))
    status=data.get('stop',{}).get('state','未知')
    required=config.get('required_checks',list(DEFAULT_REQUIRED))
    if not isinstance(required,list) or not required:raise ValueError('required_checks 必须为非空数组')
    always=['extraction','analysis_scope','model_settings','integrity']+(['energy_ratio','mesh'] if explicit else [])
    required=list(dict.fromkeys(always+required))
    if status=='求解日志显示完成':
        failures=[x for x in checks if x['status']=='fail']
        by_id={c['id']:c for c in checks}
        missing=[name for name in required if name not in by_id or by_id[name]['status']!='pass']
        if failures:level='二级';label='完成，但检查发现异常'
        elif missing:level='二级';label='完成，检查未完成或证据不足'
        else:level='一级';label='完成，配置的检查通过'
    elif any(word in status for word in ('终止','失败','中断','致命错误')):
        level='三级';label='求解失败或未完成，详见原始状态'
    else:level='未知';label='作业状态证据不足'
    return {'level':level,'label':label,'job_status':status,'analysis_procedures':[s.get('procedure') for s in steps],
            'checks':checks,'required_checks':required,
            'limits':{'energy_ratio_threshold':limit,'displacement_window_seconds':config.get('displacement_window_seconds',0.0001),
                      'displacement_range_threshold':config.get('displacement_range_threshold')},
            'scope':'仅对配置与已保存数据进行程序检查，不代表实验验证。'}


def get_registry(path):
    path=Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema_version':1,'items':{}}


def register_baseline(path, odb, record):
    """仅首次登记；与既有记录不一致时不覆盖。"""
    path=Path(path);data=get_registry(path);key=os.path.normcase(str(Path(odb).resolve()))
    previous=data['items'].get(key)
    if previous:
        if previous['sha256']!=record['sha256']:raise ValueError('已有登记值与当前文件不一致，拒绝覆盖基准')
        return previous
    entry={**record,'path':str(Path(odb).resolve()),'registered_at':datetime.now().astimezone().isoformat(),
           'origin':'登记时当前文件；不代表原始求解后文件哈希'}
    data['items'][key]=entry;path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,path)
    return entry


def integrity_check(path, odb, before, after):
    registry=get_registry(path);key=os.path.normcase(str(Path(odb).resolve()))
    baseline=registry['items'].get(key);current=before.get(str(odb))
    return {'registry':str(path),'registered':bool(baseline),'baseline':baseline,
            'baseline_matches':baseline['sha256']==current['sha256'] if baseline and current else None,
            'unchanged_during_review':before==after,'current':current}


def late_modification(odb,log):
    """仅以日志中的明确日时比较，提示不是篡改结论。"""
    if not Path(odb).exists() or not Path(log).is_file():return None
    lines=Path(log).read_text(encoding='utf-8',errors='replace').splitlines()
    timestamps=[];terminal=[]
    for i,line in enumerate(lines,1):
        s=line.strip()
        if re.fullmatch(r'\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}',s):
            try:timestamps.append((datetime.strptime(s,'%Y/%m/%d %H:%M:%S'),i))
            except ValueError:pass
        if re.search(r'END ABAQUS/EXPLICIT ANALYSIS|ABAQUS JOB .+ COMPLETED|ABAQUS/ANALYSIS EXITED WITH ERRORS',s,re.I):terminal.append(i)
    matches=[item for item in timestamps if any(0<end-item[1]<=3 for end in terminal)]
    if not matches:return None
    ended,line=matches[-1];mtime=datetime.fromtimestamp(Path(odb).stat().st_mtime)
    return {'log_file':str(log),'line':line,'log_time':ended.isoformat(),'odb_mtime':mtime.isoformat(),
            'later_than_log_time':mtime>ended,'meaning':'时间不同仅提示核查，不代表数据被篡改。'}
