"""V3.6：按已保存样本建立能量账本和可核查的异常线索。"""
import csv
import math
from collections import defaultdict
from pathlib import Path

# 官方 ETOTAL 公式；ALLAE 等是 ALLIE 的组成部分，不能再次相加。
BALANCE = {'ALLIE': 1, 'ALLVD': 1, 'ALLFD': 1, 'ALLKE': 1, 'ALLIHE': 1,
           'ALLWK': -1, 'ALLPW': -1, 'ALLCW': -1, 'ALLMW': -1, 'ALLHF': -1}
ENERGIES = set(BALANCE) | {'ETOTAL', 'ALLAE', 'ALLSE', 'ALLPD', 'ALLCD', 'ALLDMD', 'ALLDC', 'ALLFC'}


def review_history(path):
    """同区域、同时间唯一配对；保留 CSV 行号，不把缺失量当零。"""
    path = Path(path) if path else None
    result = {'source': str(path) if path else None, 'steps': [], 'contact': [],
              'limitations': ['仅使用已保存样本；缺失或重复数据不作插值或填零。']}
    if not path or not path.is_file():
        result['limitations'].append('没有可用历程文件，无法建立能量账本。')
        return result
    energies = defaultdict(lambda: defaultdict(list))
    contacts = defaultdict(list)
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for line, row in enumerate(csv.DictReader(stream), 2):
            var = row.get('variable', '').upper().strip()
            region = row.get('region', '').strip()
            if not ((var in ENERGIES and region.upper() == 'ASSEMBLY ASSEMBLY') or var.startswith('CFNM')):
                continue
            try:
                point = {'time': float(row['step_time']), 'value': float(row['value']), 'line': line}
                if not math.isfinite(point['time']) or not math.isfinite(point['value']):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            step = row.get('step', '')
            if var.startswith('CFNM'):
                contacts[(step, region, var)].append(point)
            else:
                energies[step][var].append(point)
    for step, variables in energies.items():
        for points in variables.values():
            points.sort(key=lambda p: (p['time'], p['line']))
        # 用 ETOTAL 的最后一个唯一时间对账，若没有 ETOTAL 只展示各项末样本。
        etotal = variables.get('ETOTAL', [])
        time = etotal[-1]['time'] if etotal else None
        matched = {}
        duplicate = []
        if time is not None:
            for var, points in variables.items():
                found = [p for p in points if p['time'] == time]
                if len(found) == 1:
                    matched[var] = found[0]
                elif len(found) > 1:
                    duplicate.append(var)
        missing = [var for var in BALANCE if var not in matched]
        subtotal_terms = {var: matched[var] for var in BALANCE if var in matched}
        subtotal = sum(BALANCE[var] * point['value'] for var, point in subtotal_terms.items())
        complete = not missing and 'ETOTAL' in matched
        first_etotal = etotal[0] if etotal and sum(p['time'] == etotal[0]['time'] for p in etotal) == 1 else None
        work = matched.get('ALLWK')
        work_scale = None
        if first_etotal and 'ETOTAL' in matched and work and abs(work['value']) > 1e-30:
            work_scale = {'value': abs(matched['ETOTAL']['value'] - first_etotal['value']) / abs(work['value']),
                          'start': first_etotal, 'end': matched['ETOTAL'], 'work': work}
        core = ('ALLIE', 'ALLVD', 'ALLFD', 'ALLKE', 'ALLWK', 'ETOTAL')
        partial_difference = subtotal - matched['ETOTAL']['value'] if all(v in matched for v in core) and not complete else None
        friction_over_work = (matched['ALLFD']['value'] / abs(work['value'])
                              if 'ALLFD' in matched and work and abs(work['value']) > 1e-30 else None)
        result['steps'].append({'step': step, 'region': 'Assembly ASSEMBLY', 'time': time,
            'latest': {var: points[-1] for var, points in variables.items()},
            'matched': matched, 'duplicate_at_time': sorted(duplicate), 'missing_balance_terms': missing,
            'subtotal': subtotal if subtotal_terms else None,
            'residual': subtotal - matched['ETOTAL']['value'] if complete else None, 'drift_over_end_work': work_scale,
            'partial_difference': partial_difference, 'friction_over_work': friction_over_work,
            'balance_status': '完整公式可对照，仍需结合适用性检查' if complete else '证据不足，公式项缺失或时间不匹配'})
    for (step, region, var), points in contacts.items():
        points.sort(key=lambda p: (p['time'], p['line']))
        changes = [(a['value'] - b['value'], a, b) for a, b in zip(points, points[1:]) if b['time'] > a['time']]
        fall = max(changes, key=lambda x: x[0]) if changes else None
        result['contact'].append({'step': step, 'region': region, 'variable': var, 'count': len(points),
            'minimum': min(points, key=lambda p: p['value']), 'maximum': max(points, key=lambda p: p['value']),
            'zeros': sum(p['value'] == 0 for p in points),
            'largest_sample_drop': {'change': fall[0], 'from': fall[1], 'to': fall[2]} if fall and fall[0] > 0 else None})
    return result


def report_lines(data):
    """将账本、扫描范围和待验证方向写入中文报告。"""
    v36 = data.get('v36', {})
    ledger = v36.get('history', {})
    source = ledger.get('source') or '未知历程'
    lines = ['', '## V3.6 能量账本与异常核查',
        '- ALLAE 是 ALLIE 的组成部分；下列各项按实际记录列出，不把 ALLAE 再加到 ALLIE。']
    for item in ledger.get('steps', []):
        lines.append('- %s / %s：以 ETOTAL 末样本时间 %s 对照；%s。' %
                     (item['step'], item['region'], item['time'], item['balance_status']))
        for name, point in sorted(item['latest'].items()):
            lines.append('  - %s：%.6g，t=%.9g；%s:%s。' % (name, point['value'], point['time'], source, point['line']))
        if item['missing_balance_terms']:
            lines.append('  - 公式中缺少或无法同时间唯一配对：%s；缺失不等于零，不判断完整守恒。' % '、'.join(item['missing_balance_terms']))
        if item.get('partial_difference') is not None:
            lines.append('  - 已有公式项之和减 ETOTAL = %.6g；这是不完整对照差值，未包括上列缺失项，不能直接称为能量损失或守恒破坏。' % item['partial_difference'])
        if item.get('friction_over_work') is not None:
            lines.append('  - 同时间 ALLFD / |ALLWK| = %.2f%%；为数值尺度比较，不能直接视为输入能量的分配比例。' % (100 * item['friction_over_work']))
        if item['duplicate_at_time']:
            lines.append('  - 同时间存在重复记录：%s；未参与配对。' % '、'.join(item['duplicate_at_time']))
        scale = item.get('drift_over_end_work')
        if scale:
            lines.append('  - 步内 ETOTAL 首末变化绝对值 / 末时刻 ALLWK 绝对值 = %.2f%%；%s:%d、%d、%d。此尺度仅供观察，不是通用通过标准。' %
                         (100 * scale['value'], source, scale['start']['line'], scale['end']['line'], scale['work']['line']))
        if item['residual'] is not None:
            lines.append('  - 完整公式计算值减 ETOTAL = %.6g；需结合能量定义、精度和分析类型复核。' % item['residual'])
    if not ledger.get('steps'):
        lines.append('- 全模型能量历程不可用，不能判断能量平衡。')
    checks = {item.get('id'): item for item in data.get('assessment', {}).get('checks', [])}
    energy_check = checks.get('energy_ratio', {})
    lines += ['', '## 异常的可能原因与验证步骤']
    if energy_check.get('status') == 'fail':
        lines += ['- **人工能量超过配置的筛查线。** 可能涉及单元形式、网格、沙漏控制或接触等；当前尚不能确定根因。',
                  '  先核对发生时间及相关区域，检查减缩积分单元、控制设置和网格；再选一个因素做同工况对照，比较人工能量、变形与接触力。',
                  '  控制方式的改变可能影响刚度与计算成本；未显式指定控制不等于未启用默认控制。']
    else:
        lines.append('- 人工能量未发现已配置规则的超线结论，或数据不足；应结合能量绝对值和时间曲线检查。')
    if any(item.get('drift_over_end_work') for item in ledger.get('steps', [])):
        lines.append('- **能量变化待核查：** 核对 ETOTAL 曲线与各能量项、时间及输出；先补齐适用公式项，再确定尺度和判据，不把目前缺失项当作零。')
    if any('DISTORT' in x.get('message', '').upper() for x in data.get('logs', {}).get('diagnostics', [])):
        lines.append('- **日志有畸变相关警告：** 按日志和单元集定位初始网格或求解阶段的警告，区分初始质量与运行中翻转，再决定是否局部改网格。')
    lines += ['', '## 接触力变化与观看提示']
    for item in ledger.get('contact', []):
        minimum = item['minimum']
        lines.append('- %s / %s / %s：%d 个样本，零值 %d；最小 %.6g（t=%.9g，%s:%d）。' %
                     (item['step'], item['region'], item['variable'], item['count'], item['zeros'], minimum['value'], minimum['time'], source, minimum['line']))
        drop = item['largest_sample_drop']
        if drop:
            lines.append('  - 相邻保存样本最大下降 %.6g，t=%.9g → %.9g；%s:%d、%d。先核对时间、载荷和接触面，周期振荡本身不等于异常。' %
                         (drop['change'], drop['from']['time'], drop['to']['time'], source, drop['from']['line'], drop['to']['line']))
    if not ledger.get('contact'):
        lines.append('- 没有可用 CFNM 历程；不代表没有接触。')
    summary = data.get('odb_summary') or {}
    mesh = summary.get('mesh', [])
    saved = sum(s.get('frame_count', 0) for s in summary.get('steps', []))
    lines += ['', '## 网格检查覆盖范围',
              '- 模式 %s：检查 %d 个保存帧，ODB 共记录 %d 个保存帧；不覆盖帧间增量。' % (data.get('scan', '未知'), len(mesh), saved),
              '- 网格几何检查目前仅支持 C3D8/C3D8R；跳过的其他类型和数据缺失数量见下方各帧统计。未扫描不等于合格。',
              '- 建议先在动画中查看上列能量或接触力变化时间附近及日志警告位置，再结合实际保存帧判断。']
    return lines
