"""Read-only Abaqus log inspection. All reported quantities include evidence."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

FLOAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
ROW = re.compile(rf"^\s*(\d+)\s+({FLOAT})\s+({FLOAT})\s+(\d+:\d+:\d+(?:\.\d+)?)\s+({FLOAT})\s+(\d+)\s+({FLOAT})\s+({FLOAT})\s*$")
STEP = re.compile(r"^\s*STEP\s+(\d+)\s+ORIGIN\s+(\S+)", re.I)
INSTANCE = re.compile(r"^\s*INSTANCE WITH CRITICAL ELEMENT\s*:\s*(\S+)", re.I)
DIAG = re.compile(r"^\s*\*{2,}\s*(WARNING|ERROR)\b\s*[:\-]?\s*(.*)", re.I)
MESSAGE_HEADER = re.compile(r"^\s*\*{2,}\s*[A-Z]+\b", re.I)
STATUS = re.compile(r'THE ANALYSIS HAS COMPLETED SUCCESSFULLY|ABAQUS JOB .+ COMPLETED|THE ANALYSIS HAS NOT BEEN COMPLETED|ANALYSIS HAS BEEN TERMINATED|ABAQUS JOB .+ (?:ABORTED|TERMINATED)|PROCESS TERMINATED BY EXTERNAL REQUEST|ABAQUS(?:/\w+)?(?: ANALYSIS)? EXITED WITH (?:AN? )?ERRORS?|DATACHECK|DATA CHECK|THE PROGRAM HAS DISCOVERED\s+\d+\s+FATAL ERRORS|EXECUTION IS TERMINATED', re.I)


def read_lines(path):
    data = path.read_bytes()
    for encoding in ('utf-8-sig', 'gb18030', 'cp1252'):
        try:
            return data.decode(encoding).splitlines(), encoding
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace').splitlines(), 'utf-8-replacement'


def source(path, line, text):
    return {'file': str(path), 'line': line, 'text': text.strip()}


def numeric(s):
    return float(s.replace('D', 'E').replace('d', 'e'))


def parse_sta(path, lines):
    records, step, table = [], None, False
    for n, line in enumerate(lines, 1):
        match = STEP.match(line)
        if match:
            step, table = int(match.group(1)), False
        if 'STABLE' in line.upper() and 'CRITICAL' in line.upper():
            table = True
        if table:
            match = ROW.match(line)
            if match:
                g = match.groups()
                records.append({'increment': int(g[0]), 'step': step,
                    'step_time': numeric(g[1]), 'total_time': numeric(g[2]),
                    'stable_increment': numeric(g[4]), 'element': int(g[5]),
                    'instance': None, 'evidence': source(path, n, line)})
                if n < len(lines):
                    inst = INSTANCE.match(lines[n])
                    if inst:
                        records[-1]['instance'] = inst.group(1)
                        records[-1]['instance_evidence'] = source(path, n + 1, lines[n])
    return records


def diagnostics(path, lines):
    out, i = [], 0
    while i < len(lines):
        match = DIAG.match(lines[i])
        if not match:
            i += 1
            continue
        start, parts, severity = i, [lines[i].strip()], match.group(1).lower()
        i += 1
        while i < len(lines) and lines[i].strip() and not MESSAGE_HEADER.match(lines[i]):
            if not lines[i][0].isspace():
                break
            parts.append(lines[i].strip())
            i += 1
        out.append({'severity': severity, 'message': ' '.join(parts),
                    'file': str(path), 'line_start': start + 1, 'line_end': i})
    return out


def diagnose(directory, job):
    # Strict resolution requires extra directory rights on some Windows sandboxes.
    directory = Path(directory).resolve(strict=False)
    if not directory.is_dir():
        raise ValueError('输入路径必须是目录')
    if not job or Path(job).name != job or job in ('.', '..'):
        raise ValueError('作业名必须是文件名，不含目录或扩展名')
    matches = {p.suffix.lower(): p for p in directory.iterdir()
               if p.is_file() and p.stem.lower() == job.lower()
               and p.suffix.lower() in ('.sta', '.msg', '.dat', '.log', '.lck')}
    if not any(k in matches for k in ('.sta', '.msg', '.dat', '.log')):
        raise FileNotFoundError(f'没有找到作业 {job} 的 STA/MSG/DAT/LOG 文件')
    files, findings, records, status_evidence = {}, [], [], []
    for suffix in ('.sta', '.msg', '.dat', '.log'):
        if suffix not in matches:
            continue
        path = matches[suffix]
        lines, encoding = read_lines(path)
        files[suffix] = {'path': str(path), 'encoding': encoding, 'line_count': len(lines)}
        findings.extend(diagnostics(path, lines))
        if suffix == '.sta':
            records = parse_sta(path, lines)
        for n, line in enumerate(lines, 1):
            if STATUS.search(line):
                status_evidence.append(source(path, n, line))
    # Completion or failure must be evidenced by STA/LOG; DAT diagnostics alone are not a final job state.
    definitive = [x for x in status_evidence if Path(x['file']).suffix.lower() in ('.sta', '.log')]
    outcomes = []
    for item in definitive:
        upper = item['text'].upper()
        if re.search(r'NOT BEEN COMPLETED|TERMINATED|ABORTED|EXITED WITH (?:AN? )?ERRORS?', upper):
            outcomes.append('failure')
        elif 'COMPLETED' in upper:
            outcomes.append('complete')
    final = definitive[-1] if definitive else None
    text = final['text'].upper() if final else ''
    if len(set(outcomes)) > 1:
        state = '未知（不同记录的结束状态相互矛盾，请核对作业版本）'
    elif any('PROCESS TERMINATED BY EXTERNAL REQUEST' in x['text'].upper() for x in definitive):
        state = '日志显示外部请求终止（具体请求来源未知）'
    elif re.search(r'ABORTED|NOT BEEN COMPLETED|EXITED WITH (?:AN? )?ERRORS?', text):
        state = '失败或中断（日志未区分原因）'
    elif 'TERMINATED' in text:
        state = '日志显示终止（原因待核实）'
    elif 'DATACHECK' in text or 'DATA CHECK' in text:
        state = '数据检查相关，完整分析状态未知'
    elif 'COMPLETED' in text:
        # A data-check log and a stale STA can coexist; do not assert full analysis completed.
        checks = [x for x in status_evidence if re.search(r'DATACHECK|DATA CHECK', x['text'], re.I)]
        if checks:
            definitive.extend(x for x in checks if x not in definitive)
        state = '数据检查相关，完整分析状态未知' if checks else '求解日志显示完成'
    else:
        dat_evidence = [x for x in status_evidence if Path(x['file']).suffix.lower() == '.dat']
        if (any(re.search(r'THE PROGRAM HAS DISCOVERED\s+\d+\s+FATAL ERRORS', x['text'], re.I) for x in dat_evidence)
                and any(re.search(r'EXECUTION IS TERMINATED', x['text'], re.I) for x in dat_evidence)):
            state = '输入处理遇致命错误并终止（整个作业最终状态未独立核实）'
            definitive.extend(dat_evidence)
        else:
            state = '未知（日志缺少可靠终态；不凭锁文件推断运行中）'
    selected = {'first_recorded': records[0] if records else None,
                'minimum_recorded': min(records, key=lambda r: r['stable_increment']) if records else None,
                'last_recorded': records[-1] if records else None}
    return {'schema_version': '1.0', 'job': job, 'input_directory': str(directory),
            'generated_at': datetime.now().astimezone().isoformat(), 'files': files,
            'missing_files': [ext for ext in ('.sta', '.msg', '.dat', '.log') if ext not in files],
            'status': {'value': state, 'evidence': definitive, 'lock_file_present': '.lck' in matches},
            'stable_increment': {'scope': 'STA 中已记录的 Explicit 表格采样，非全程最小值',
                                 'count': len(records), **selected, 'records': records},
            'diagnostics': findings,
            'limitations': ['不读取 ODB、不验证物理可靠性、不判断是否达到工艺目标',
                            '不核实进程，锁文件可能过期', '首条记录未必是求解初始值']}


def report_text(result):
    status = result['status']
    lines = ['# Abaqus 日志诊断报告', '', f"作业：{result['job']}",
             f"输入目录：{result['input_directory']}", f"状态：{status['value']}",
             f"锁文件存在：{'是' if status['lock_file_present'] else '否'}（不代表运行状态）",
             f"未找到：{', '.join(result['missing_files']) or '无'}（缺失文件中的信息无法判断）", '']
    for e in status['evidence']:
        lines.append(f"- 状态证据：{e['file']}:{e['line']} — {e['text']}")
    lines += ['', '## 稳定时间增量', result['stable_increment']['scope']]
    for key, label in (('first_recorded', '首条记录'), ('minimum_recorded', '已记录最小值'), ('last_recorded', '最后记录')):
        r = result['stable_increment'][key]
        if r:
            e = r['evidence']
            inst_source = r.get('instance_evidence')
            instance_ref = f"；实例来源：{inst_source['file']}:{inst_source['line']}" if inst_source else ''
            lines.append(f"- {label}：{r['stable_increment']:.8g}；分析步 {r['step']}，步时间 {r['step_time']:.8g}，总时间 {r['total_time']:.8g}，实例 {r['instance'] or '未知'}，单元 {r['element']}。记录来源：{e['file']}:{e['line']}{instance_ref}")
        else:
            lines.append(f'- {label}：未知，未识别到 Explicit 增量记录。')
    lines += ['', '## 警告与错误']
    counts = Counter(d['severity'] for d in result['diagnostics'])
    lines.append(f"记录数：警告 {counts['warning']}，错误 {counts['error']}（不同文件可能重复）。")
    for d in result['diagnostics']:
        message = d['message']
        if len(message) > 500:
            message = message[:500] + '……（全文见 JSON 或原始日志）'
        lines.append(f"- {d['severity'].upper()} [{d['file']}:{d['line_start']}-{d['line_end']}] {message}")
    lines += ['', '## 限制'] + [f'- {x}' for x in result['limitations']]
    return '\n'.join(lines) + '\n'


