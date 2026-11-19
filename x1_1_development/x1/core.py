"""快速查询、只读核对与受控副本操作。"""
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def new_run(base, kind):
    root = Path(base).expanduser().resolve() if base else ROOT / 'runs'
    root.mkdir(parents=True, exist_ok=True)
    folder = root / (kind + '_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    folder.mkdir()
    return folder


def search(query, limit=3, mode=None):
    """无网络依赖；返回操作步骤及其出处。"""
    start = time.perf_counter()
    knowledge = json.loads((ROOT / 'kb' / 'topics.json').read_text(encoding='utf-8'))
    q = query.lower().strip()
    found = []
    for item in knowledge:
        if mode and item.get('modes') and mode not in item['modes']:
            continue
        if not mode and item.get('modes') == ['adiabatic'] and '绝热' not in q and 'adiabatic' not in q:
            continue
        words = item['keywords'] + [item['title']]
        score = sum((4 if len(w) > 2 else 2) for w in words if w.lower() in q)
        score += sum(1 for w in words if len(w) >= 2 and q in w.lower()) if len(q) >= 2 else 0
        if score:
            found.append((score, item))
    found.sort(key=lambda x: -x[0])
    return {'query': query, 'analysis_mode': mode, 'elapsed_ms': round((time.perf_counter() - start) * 1000, 2),
            'results': [item for _, item in found[:limit]]}


def sensor_requests(text):
    """按分析步识别独立历史输出块，不以名称推断传感器。"""
    lines = text.splitlines(keepends=True)
    step = None
    blocks = []
    current = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith('*') or s.startswith('**'):
            if current is not None and s and not s.startswith('**'):
                current['data'].append(s)
            continue
        keyword = s[1:].split(',')[0].strip().lower()
        params = {p.split('=', 1)[0].strip().lower(): (p.split('=', 1)[1].strip() if '=' in p else True)
                  for p in s.split(',')[1:] if p.strip()}
        if keyword == 'step':
            step = params.get('name')
        elif keyword == 'end step':
            step = None
        if keyword == 'output':
            current = {'step': step, 'line': i + 1, 'params': params, 'data': [], 'kind': None, 'region': None}
            blocks.append(current)
        elif current is not None and keyword in ('contact output', 'node output', 'element output'):
            current['kind'] = keyword
            current['region'] = params.get('surface') or params.get('nset') or params.get('elset')
        elif keyword not in ('contact output', 'node output', 'element output'):
            current = None
    return lines, blocks


def audit_sensor(path):
    path = Path(path).resolve()
    text = path.read_bytes().decode('latin-1')
    _, blocks = sensor_requests(text)
    return {'input': str(path), 'sha256': digest(path), 'history_outputs':
            [{**b, 'registered_sensor': 'history' in b['params'] and 'sensor' in b['params'],
              'sensor_name': b['params'].get('name')} for b in blocks if 'history' in b['params']]}


def repair_sensor(path, output_dir, name, surface, steps):
    """将明确指定的单变量 CFNM 输出注册为传感器，生成独立 INP。"""
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name):
        raise ValueError('传感器名须以字母开头，仅包含字母、数字或下划线。')
    source = Path(path).resolve()
    original_hash = digest(source)
    raw = source.read_bytes()
    text = raw.decode('latin-1')
    if re.search(r'(?im)^\s*\*include\s*,', text):
        raise ValueError('输入文件包含 INCLUDE；请先处理相对路径，避免副本缺失依赖。')
    lines, blocks = sensor_requests(text)
    requested = {x.casefold() for x in steps}
    chosen = [b for b in blocks if (b['step'] or '').casefold() in requested
              and b['kind'] == 'contact output' and str(b['region']).casefold() == surface.casefold()
              and [v.strip().upper() for row in b['data'] for v in row.split(',') if v.strip()] == ['CFNM']
              and 'history' in b['params']]
    if len(chosen) != len(requested) or {b['step'].casefold() for b in chosen} != requested:
        raise ValueError('每个指定分析步必须恰有一个指定表面的单变量 CFNM 历程输出。')
    for b in blocks:
        if ('sensor' in b['params'] and str(b['params'].get('name', '')).casefold() == name.casefold()
                and b not in chosen and (b['step'] or '').casefold() in requested):
            raise ValueError('同一分析步已存在同名传感器。')
    changed = []
    for b in chosen:
        if 'sensor' in b['params']:
            if str(b['params'].get('name', '')).casefold() != name.casefold():
                raise ValueError('该输出已注册为其他传感器。')
            continue
        i = b['line'] - 1
        old = lines[i]
        lines[i] = re.sub(r'(?i)(\*output\s*,\s*history)', r'\1, sensor, name=' + name, old, count=1)
        changed.append(b['line'])
    folder = new_run(output_dir, 'sensor_repair')
    target = folder / source.name
    target.write_bytes(''.join(lines).encode('latin-1'))
    diff = ''.join(difflib.unified_diff(text.splitlines(keepends=True), ''.join(lines).splitlines(keepends=True),
                                        fromfile=str(source), tofile=str(target)))
    (folder / '修改对照.diff').write_text(diff, encoding='utf-8')
    result = {'input': str(source), 'output': str(target), 'changed_lines': changed, 'steps': steps,
              'surface': surface, 'sensor': name, 'source_sha256_before': original_hash,
              'source_sha256_after': digest(source), 'output_sha256': digest(target)}
    (folder / '结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / '报告.md').write_text('# 传感器修复记录\n\n输入：`%s`\n\n输出：`%s`\n\n分析步：%s；表面：%s；变量：CFNM；传感器：%s。\n\n修改源文件行：%s。原文件 SHA-256 前后%s。\n\n输出为 INP 副本，尚需用户在 Abaqus 中进行数据检查。\n' %
        (source, target, ', '.join(steps), surface, name, changed,
         '一致' if result['source_sha256_before'] == result['source_sha256_after'] else '不一致'), encoding='utf-8')
    return result


def cae_operation(source, output_dir, action, model, part=None, size=None, abaqus='abaqus'):
    """后台以 CAE 工作副本检查或重划指定零件，不触碰求解作业。"""
    source = Path(source).resolve()
    original_hash = digest(source)
    folder = new_run(output_dir, 'cae_' + action)
    working = folder / ('model_working' + source.suffix)
    shutil.copy2(source, working)
    request = {'action': action, 'model_copy': str(working), 'model': model, 'part': part, 'size': size}
    (folder / 'request.json').write_text(json.dumps(request, ensure_ascii=True, indent=2), encoding='ascii')
    worker = Path(__file__).parent / 'worker_cae.py'
    cmd = [abaqus, 'cae', 'noGUI=' + str(worker)]
    started = time.perf_counter()
    try:
        process = subprocess.run(cmd, cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors='replace', timeout=600, shell=(os.name == 'nt'))
        log = process.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log = str(exc)
    (folder / '后台运行日志.txt').write_text(log, encoding='utf-8')
    result_file = folder / 'worker_result.json'
    result = json.loads(result_file.read_text(encoding='utf-8')) if result_file.exists() else {'ok': False, 'error': log.strip()}
    result.update({'input': str(source), 'working_copy': str(working), 'action': action,
                   'source_sha256_before': original_hash, 'source_sha256_after': digest(source),
                   'elapsed_s': round(time.perf_counter() - started, 2), 'run_dir': str(folder)})
    (folder / '结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    status = '完成' if result['ok'] else '未完成'
    (folder / '报告.md').write_text('# CAE 后台%s\n\n状态：%s\n\n输入：`%s`\n\n工作副本：`%s`\n\n原文件哈希前后%s。\n\n详情见结果.json和后台运行日志.txt。\n' %
        (action, status, source, working, '一致' if original_hash == result['source_sha256_after'] else '不一致'), encoding='utf-8')
    return result
