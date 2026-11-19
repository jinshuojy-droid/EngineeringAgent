"""跨分析步载荷、传感器和结果关联：只读、保留证据。"""
import ast
import csv
import math
from pathlib import Path
import re

from v3_inp import parse_inp

LOAD_DOC = 'https://docs.software.vt.edu/abaqusv2026/English/SIMACAEPRCRefMap/simaprc-c-loading.htm'


def resolve_sources(directory, job, inp=None, user=None):
    """以用户指定、作业命令文件、同名输入依次确定来源。"""
    directory = Path(directory)
    command = directory / (job + '.com')
    options = {}
    if command.is_file():
        for no, line in enumerate(command.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            match = re.match(r"\s*['\"](input|user)['\"]\s*:\s*(.+?),?\s*$", line, re.I)
            if match:
                try:
                    value = ast.literal_eval(match.group(2).rstrip(','))
                    if isinstance(value, str) and value:
                        options[match.group(1).lower()] = (value, no)
                except (ValueError, SyntaxError):
                    pass
    def choose(explicit, key, default=None):
        value = explicit or (options.get(key) or (default,))[0]
        if not value:
            return None
        path = Path(value)
        if key == 'input' and not path.suffix:
            path = path.with_suffix('.inp')
        if not path.is_absolute():
            path = directory / path
        return str(path.resolve())
    return {'inp': choose(inp, 'input', job), 'user': choose(user, 'user'),
            'command': str(command) if command.is_file() else None,
            'command_lines': {key: item[1] for key, item in options.items()},
            'source_note': '命令文件记录路径；文件可能在求解后被修改，不能单凭路径证明原始内容。'}


def analyze_input(path, user_path=None):
    """识别 USER 集中力跨步及可静态识别的传感器。"""
    if not path or not Path(path).is_file():
        return {'status': 'unknown', 'findings': [], 'reason': '没有找到实际 INP，无法核对模型。'}
    parsed = parse_inp(path)
    records = parsed['records']
    steps = [s for s in records['steps'] if s['keyword'] == 'step']
    amps = {str(a['parameters'].get('name', '')).casefold(): a for a in records['amplitudes']}
    loads = [r for r in records['loads'] if r['keyword'] == 'cload']
    findings = []
    for index, step in enumerate(steps[:-1]):
        name = step['scope']['step']
        next_name = steps[index + 1]['scope']['step']
        for load in loads:
            if load['scope']['step'] != name:
                continue
            amp = amps.get(str(load['parameters'].get('amplitude', '')).casefold())
            if not amp or str(amp['parameters'].get('definition', '')).upper() != 'USER':
                continue
            later = [r for r in loads if r['scope']['step'] == next_name]
            for row in load['data']:
                fields = [s.strip().casefold() for s in row['text'].split(',')]
                if len(fields) < 3:
                    continue
                found = any(len(q['text'].split(',')) >= 3 and
                            [x.strip().casefold() for x in q['text'].split(',')[:2]] == fields[:2]
                            for item in later for q in item['data'])
                ambiguous = any(str(item['parameters'].get('op', '')).upper() == 'NEW' for item in later)
                if not found:
                    findings.append({'id': 'user_load_next_step',
                        'status': 'unknown' if not parsed['complete'] or ambiguous else 'warning',
                        'summary': '%s 的 USER 幅值集中力 %s / 自由度 %s 在 %s 未找到同项重新定义。' % (name, fields[0], fields[1], next_name),
                        'action': '核对后续步是否需要保持载荷及实际幅值规则；若需要保持，检查在后续步显式重新定义，并做数据检查。',
                        'evidence': [load['evidence'], row['evidence'], amp['evidence'], steps[index + 1]['evidence']],
                        'basis': LOAD_DOC})
    outputs = [r for r in records.get('outputs', []) if r['keyword'] == 'output' and 'sensor' in r['parameters']]
    sensors = {}
    for output in outputs:
        sensor = str(output['parameters'].get('name', '')).casefold()
        sensors.setdefault(sensor, []).append(output)
    output_records=records.get('outputs', [])
    source = Path(user_path) if user_path else None
    if source and source.is_file():
        lines = source.read_text(encoding='utf-8', errors='replace').splitlines()
        for line_no, line in enumerate(lines, 1):
            if line.lstrip().startswith(('C ', 'c ', '*', '!')):
                continue
            for match in re.finditer(r"V?GETSENSORVALUE\s*\(\s*['\"]([^'\"]+)['\"]", line, re.I):
                sensor = match.group(1)
                present = sensors.get(sensor.casefold(), [])
                missing = [step['scope']['step'] for step in steps if not any(s['scope']['step'] == step['scope']['step'] for s in present)]
                descriptions=[]
                for entry in present:
                    position=output_records.index(entry)
                    for detail in output_records[position+1:]:
                        if detail['keyword']=='output':break
                        if detail['keyword'] in ('node output','contact output'):
                            descriptions.append('%s %s %s'%(entry['scope']['step'],detail['parameters'],','.join(d['text'] for d in detail['data'])))
                findings.append({'id': 'sensor_registration',
                    'status': 'unknown' if missing else 'observed',
                    'summary': '子程序传感器 %s：%s。%s' % (sensor, '未在以下分析步找到注册：' + ', '.join(missing) if missing else '各分析步均找到同名注册', '；'.join(descriptions)),
                    'action': '核对传感器实际启用步骤、输出变量、区域和 VUAMP 调用条件。',
                    'evidence': [{'file': str(source), 'line': line_no, 'text': line.strip()}] + [x['evidence'] for x in present]})
    return {'status': 'complete' if parsed['complete'] else 'partial', 'findings': findings,
            'files': parsed['files'], 'warnings': parsed['warnings'],
            'scope': '只核对显式 CLOAD 和可静态识别的传感器名称；未完整解释全部 Abaqus 语义。'}


def history_findings(path):
    """统计每个接触力区域的零值比例，不推断接触机理。"""
    if not path or not Path(path).is_file():
        return []
    groups = {}; displacements = {}
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            variable=row.get('variable', '').upper()
            if not variable.startswith('CFNM') and variable not in ('U1','U2','U3'):
                continue
            try:
                value = float(row['value']); time = float(row['step_time'])
            except (ValueError, KeyError):
                continue
            if not math.isfinite(value) or not math.isfinite(time):continue
            if variable in ('U1','U2','U3'):
                key=(row['step'],row['region'],variable)
                item=displacements.setdefault(key,{'first':None,'last':None,'min':None,'max':None,'count':0})
                point={'time':time,'value':value,'file':str(path),'line':line}
                if item['first'] is None or time<item['first']['time']:item['first']=point
                if item['last'] is None or time>item['last']['time']:item['last']=point
                if item['min'] is None or value<item['min']['value']:item['min']=point
                if item['max'] is None or value>item['max']['value']:item['max']=point
                item['count']+=1
                continue
            key = (row['step'], row['region'], row['variable'])
            group = groups.setdefault(key, {'count': 0, 'zeros': 0, 'first': None, 'last': None})
            group['count'] += 1
            group['zeros'] += value == 0.0
            point = {'time': time, 'value': value, 'file': str(path), 'line': line}
            if group['first'] is None: group['first'] = point
            group['last'] = point
    result = []
    regions={key[1] for key in displacements}
    if len(regions)==1:
        for (step,region,variable),item in displacements.items():
            if item['count']<2:continue
            result.append({'id':'displacement_observation','status':'observed',
                'summary':'%s / %s / %s：首末值 %.6g → %.6g，范围 %.6g 至 %.6g。' %
                    (step,region,variable,item['first']['value'],item['last']['value'],item['min']['value'],item['max']['value']),
                'action':'核对该区域身份、坐标方向和单位，并结合载荷及接触历程解释。',
                'evidence':[item['first'],item['last'],item['min'],item['max']]})
    for (step, region, variable), item in groups.items():
        if item['count'] >= 10 and item['zeros'] / item['count'] >= 0.9:
            result.append({'id': 'contact_zero_samples', 'status': 'observation',
                'summary': '%s / %s / %s：%d/%d 个已保存样本为零。' % (step, region, variable, item['zeros'], item['count']),
                'action': '结合施加载荷阶段、接触压力与位移检查该时段；确认该区域对应目标接触面。',
                'evidence': [item['first'], item['last']]})
    return result


def correlate(findings):
    """列出值得联合复核的证据；不假设数据关联已被证明。"""
    loads=[f for f in findings if f['id']=='user_load_next_step']
    contacts=[f for f in findings if f['id']=='contact_zero_samples']
    if not loads or not contacts:
        return []
    return [{'id':'load_contact_correlation','status':'observation',
             'summary':'发现 USER 载荷跨步待核查项，同时有接触力零值集中的历程。',
             'action':'核对历程、INP 是否属于同次作业及载荷与接触区域是否对应；再检查载荷传递并比较修正后的曲线。',
             'evidence':loads[0]['evidence'][:2]+contacts[0]['evidence']}]
