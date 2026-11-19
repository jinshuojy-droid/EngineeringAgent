"""V3 联合诊断：区分测量事实、程序规则和待验证假设。"""
import csv
import json
import math
from pathlib import Path

from history_analysis import read_history, stats, energy_ratios
from v3_tracks import same_odb


def assess(logs, track):
    facts, rules, hypotheses, recommendations = [], [], [], []
    records = logs.get('stable_increment', {}).get('records', [])
    minimum = logs.get('stable_increment', {}).get('minimum_recorded')
    if minimum and minimum['step'] is not None:
        same_step = [r for r in records if r['step'] == minimum['step'] and r['stable_increment'] > 0]
        if same_step:
            first = same_step[0]
            ratio = minimum['stable_increment'] / first['stable_increment']
            facts.append({'type': 'recorded_increment_ratio', 'ratio': ratio, 'first': first, 'minimum': minimum,
                          'note': '同一分析步首条采样与最小采样对比，不保证覆盖所有增量。'})
    warnings = logs.get('diagnostics', [])
    for family, terms in [('penetration', ('PENETRAT',)), ('distortion', ('DISTORT', 'WARPING')),
                          ('contact_constraint', ('KINEMATIC CONTACT', 'KINEMATIC CONTACT CONSTRAINT'))]:
        matches = [w for w in warnings if any(t in w['message'].upper() for t in terms)]
        if matches:
            facts.append({'type': family + '_warning', 'evidence': matches})
    if track:
        frames = [f for f in track['frames'] if f['geometry'].get('deformed') is not None
                  and f['geometry'].get('status') in ('ok', 'nonpositive_jacobian')]
        invalid = [f for f in frames if f['geometry'].get('status') == 'nonpositive_jacobian']
        if invalid:
            f = invalid[0]
            rules.append({'type': 'nonpositive_sampled_jacobian', 'step': f['step'], 'frame_index': f['frame_index'],
                          'step_time': f['step_time'], 'evidence': f['displacements'],
                          'interpretation': '在节点坐标与 U 坐标系一致的前提下，九个采样点存在非正或退化雅可比；须核对坐标系，不能仅此推断起因。'})
        if frames:
            f = min((f for f in frames if f['geometry']['deformed']['minimum_scaled_jacobian'] is not None),
                    key=lambda x: x['geometry']['deformed']['minimum_scaled_jacobian'], default=None)
            if f:
                facts.append({'type': 'min_saved_scaled_jacobian', 'step': f['step'], 'frame_index': f['frame_index'],
                              'step_time': f['step_time'], 'value': f['geometry']['deformed']['minimum_scaled_jacobian'],
                              'evidence': f['displacements']})
    if any(f['type'] == 'penetration_warning' for f in facts):
        hypotheses.append({'question': '穿透与变形的先后顺序和可能联系', 'status': '待验证',
                           'reason': '日志有穿透警告，但尚未证明对应同一位置或先后顺序，也不能证明因果。'})
        recommendations.append({'priority': 1, 'action': '复核警告节点集、对应接触面与网格，在相邻保存帧核对间隙和变形。',
                                'tradeoff': '稀疏场输出可能不能确定首次穿透的准确时间。',
                                'verification': '核对同一节点/接触面的来源、步时间及输出位置；仍缺数据时标记未知。'})
    if any(f['type'] == 'distortion_warning' for f in facts) or any(r['type'] == 'nonpositive_sampled_jacobian' for r in rules):
        hypotheses.append({'question': '网格、接触及约束是否共同影响单元几何变化', 'status': '待验证',
                           'reason': '几何变化或警告不能单独区分载荷、网格、接触、材料及约束的影响。'})
        recommendations.append({'priority': 2, 'action': '检查关键单元初始网格、相邻接触和约束；根据证据选择单变量对照。',
                                'tradeoff': '网格细化会增加自由度并可能缩小稳定增量；改变接触或质量缩放会改变数值响应。',
                                'verification': '对比保存帧几何指标、STA 采样增量、能量和接触输出，观察物理响应是否同步合理。'})
    recommendations.append({'priority': 3, 'action': '核对 ALLAE、ALLIE、ALLKE、ETOTAL、ALLWK 等实际能量历程及模型中的沙漏控制。',
                            'tradeoff': '更改单元或沙漏控制可能改变刚度、能量耗散及计算成本。',
                            'verification': '在同一工况和可比采样时间对照能量及局部变形；单一能量比例不足以证明可信。'})
    return {'facts': facts, 'rules': rules, 'hypotheses': hypotheses, 'recommendations': recommendations}


def energy_summary(extracted, energy_file=None, odb_file=None):
    path = Path(energy_file) if energy_file else Path(extracted) / 'history.csv'
    if odb_file:
        with path.open(encoding='utf-8', newline='') as f:
            for line, row in enumerate(csv.DictReader(f), 2):
                if not same_odb(row.get('odb_file'), odb_file):
                    raise ValueError('能量 CSV ODB 来源不一致，第 %d 行' % line)
    groups = read_history(path)
    result = []
    for step in sorted({k[0] for k in groups}):
        for region in sorted({k[1] for k in groups if k[0] == step}):
            region_rows = [r for (s, reg, _), rows in groups.items() if s == step and reg == region for r in rows]
            if any(r.get('domain', '').upper() != 'TIME' for r in region_rows):
                result.append({'step': step, 'region': region, 'source': str(path), 'variables': {},
                               'allae_allie_last': None, 'allae_allie_max': None,
                               'ratio_method': '分析域非 TIME 或未知，不按时间历程解释。'})
                continue
            measurements = {v: stats(groups.get((step, region, v), []))
                            for v in ('ALLKE', 'ALLIE', 'ALLAE', 'ETOTAL', 'ALLWK', 'ALLSE')}
            if not any(measurements.values()):
                continue
            energy_rows = groups.get((step, region, 'ALLAE'), []) + groups.get((step, region, 'ALLIE'), [])
            ratio = (energy_ratios(groups, step, region) if not any(r['segment'] for r in energy_rows)
                     else {'last': None, 'max': None, 'method': '存在时间回退；不同变量段无法证明对应，拒绝自动配比。'})
            result.append({'step': step, 'region': region, 'source': str(path), 'variables': measurements,
                           'allae_allie_last': ratio['last'], 'allae_allie_max': ratio['max'],
                           'ratio_method': ratio['method']})
    return result


def captured_inputs(paths):
    captured = {}
    for path in paths:
        p = Path(path)
        try:
            st = p.stat()
            captured[str(p)] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
        except OSError:
            captured[str(p)] = None
    return captured


def write_track_csv(path, track):
    if not track:
        return
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['instance', 'element', 'element_type', 'step', 'frame_index', 'step_time', 'total_time',
                         'status', 'min_scaled_jacobian', 'shortest_edge', 'peeq_values_json', 'contact_values_json', 'field_evidence_json'])
        for frame in track['frames']:
            geometry = frame['geometry']; metrics = geometry.get('deformed') or {}
            writer.writerow([track['instance'], track['element'], track.get('element_definition', {}).get('type'),
                             frame['step'], frame['frame_index'], frame['step_time'], frame['total_time'], geometry.get('status'),
                             metrics.get('minimum_scaled_jacobian'), metrics.get('shortest_edge'),
                             json.dumps(frame['peeq'], ensure_ascii=False), json.dumps(frame['contact'], ensure_ascii=False),
                             json.dumps([d.get('source') for d in frame['displacements']], ensure_ascii=False)])


def plot_results(out, logs, track, energy):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return [], '未安装 Matplotlib，未生成图；CSV/JSON 不受影响。'
    plots = []
    records = logs.get('stable_increment', {}).get('records', [])
    if records:
        fig, ax = plt.subplots()
        for step in sorted({r['step'] for r in records}, key=lambda s: (s is None, s or 0)):
            series = [r for r in records if r['step'] == step]
            ax.plot([r['step_time'] for r in series], [r['stable_increment'] for r in series], label='Step '+str(step))
        ax.set_xlabel('Step time'); ax.set_ylabel('Recorded stable increment'); ax.legend(); ax.grid(True)
        fig.tight_layout(); name = '01_stable_increment.png'; fig.savefig(out / name, dpi=150); plt.close(fig); plots.append(name)
    if track:
        rows = [r for r in track['frames'] if r['geometry'].get('deformed') is not None and r['step_time'] is not None]
        if rows:
            fig, ax = plt.subplots()
            for step in sorted({r['step'] for r in rows}):
                data = [r for r in rows if r['step'] == step and r['geometry']['deformed']['minimum_scaled_jacobian'] is not None]
                if data:
                    ax.plot([r['step_time'] for r in data], [r['geometry']['deformed']['minimum_scaled_jacobian'] for r in data], label=step)
            ax.set_xlabel('Step time'); ax.set_ylabel('Minimum sampled scaled Jacobian'); ax.legend(); ax.grid(True)
            fig.tight_layout(); name = '02_element_geometry.png'; fig.savefig(out / name, dpi=150); plt.close(fig); plots.append(name)
        for family in ('PEEQ', 'CPRESS', 'COPEN', 'CSLIP', 'CDISP'):
            per_step = {}
            for frame in track['frames']:
                if frame['step_time'] is None:
                    continue
                entries = frame['peeq'] if family == 'PEEQ' else [x for x in frame.get('contact', []) if x['variable'].upper().startswith(family)]
                for name in sorted({e.get('variable', family) for e in entries}):
                    values = [v for e in entries if e.get('variable', family) == name for v in (e['values'] or [])
                              if isinstance(v, (int, float)) and math.isfinite(v)]
                    if values:
                        per_step.setdefault((frame['step'], name), []).append((frame['step_time'], max(values)))
            if per_step:
                fig, ax = plt.subplots()
                for (step, name), samples in per_step.items():
                    ax.plot([x[0] for x in samples], [x[1] for x in samples], label=step+' '+name)
                ax.set_xlabel('Step time'); ax.set_ylabel('Sample maximum component ' + family); ax.legend(fontsize='small'); ax.grid(True)
                fig.tight_layout(); name = 'track_' + family.lower() + '.png'; fig.savefig(out / name, dpi=150); plt.close(fig); plots.append(name)
    if energy:
        groups = read_history(energy[0]['source'])
        for item in energy:
            fig, ax = plt.subplots(); count = 0
            if not item['variables']:
                plt.close(fig)
                continue
            for var in ('ALLKE', 'ALLIE', 'ALLAE', 'ETOTAL'):
                rows = [r for r in groups.get((item['step'], item['region'], var), [])
                        if r['step_time'] is not None and r['value'] is not None and math.isfinite(r['value'])]
                for segment in sorted({r['segment'] for r in rows}):
                    points = [r for r in rows if r['segment'] == segment]
                    if points:
                        ax.plot([r['step_time'] for r in points], [r['value'] for r in points], label=var + (' seg '+str(segment) if segment else '')); count += 1
            if count:
                ax.set_xlabel('Step time'); ax.set_ylabel('Energy (model units)'); ax.legend(); ax.grid(True)
                fig.tight_layout(); name = 'energy_%02d.png' % len(plots); fig.savefig(out / name, dpi=150); plots.append(name)
            plt.close(fig)
    return plots, None


def report(data):
    lines = ['# EngineeringAgent V3.1 只读联合诊断报告', '',
             '作业：' + data['job'], '输入目录：' + data['input_directory'],
             'INP：' + str(data['inputs'].get('inp') or '未提供'),
             'ODB / 提取目录：' + str(data['inputs'].get('odb') or '未提供') + ' / ' + str(data['inputs'].get('extracted') or '未提供'),
             '', '## 一、事实与来源', '日志状态：' + data['logs'].get('status', {}).get('value', '未知')]
    for key, name in [('first_recorded', '首次采样'), ('minimum_recorded', '最小采样'), ('last_recorded', '最后采样')]:
        r = data['logs'].get('stable_increment', {}).get(key)
        if r:
            e = r['evidence']; ie = r.get('instance_evidence')
            lines.append('- %s：%s；步 %s，步时间 %s，总时间 %s；实例 %s，单元 %s。%s:%s%s' %
                         (name, r['stable_increment'], r['step'], r['step_time'], r['total_time'], r['instance'] or '未知', r['element'], e['file'], e['line'],
                          '；实例来源 %s:%s' % (ie['file'], ie['line']) if ie else ''))
    for e in data['logs'].get('diagnostics', []):
        if any(k in e['message'].upper() for k in ('CONTACT', 'PENETRAT', 'DISTORT', 'MASS', 'ERROR')):
            lines.append('- 日志 %s：%s（%s:%s-%s）' % (e['severity'], e['message'], e['file'], e['line_start'], e['line_end']))
    for energy in data['energy']:
        if not energy['variables']:
            lines.append('- %s / %s 能量数据未按时间历程分析：%s' % (energy['step'], energy['region'], energy['ratio_method']))
        for var, evidence in energy['variables'].items():
            if evidence and var in ('ETOTAL', 'ALLWK', 'ALLSE'):
                last_sample = evidence['last']
                lines.append('- %s / %s %s 末个记录值 %s，步时间 %s；来源 %s:%s。' %
                             (energy['step'], energy['region'], var, last_sample['value'], last_sample['step_time'], energy['source'], last_sample['csv_line']))
                if var == 'ETOTAL':
                    low, high = evidence['minimum'], evidence['maximum']
                    lines.append('- %s 的 ETOTAL 已记录范围 %s 至 %s，分别来自 %s:%s 和 %s；非全程极值，需结合外功和物理过程解释。' %
                                 (energy['step'], low['value'], high['value'], energy['source'], low['csv_line'], high['csv_line']))
        last = energy['allae_allie_last']
        if last:
            a, b = last['allae'], last['allie']
            lines.append('- %s / %s 末个同时间能量比 ALLAE/ALLIE = %.6g；步时间 %s；来源 %s:%s、%s。' %
                         (energy['step'], energy['region'], last['ratio'], a['step_time'], energy['source'], a['csv_line'], b['csv_line']))
        else:
            lines.append('- %s / %s 无可确认的 ALLAE/ALLIE 配对；%s' % (energy['step'], energy['region'], energy['ratio_method']))
    tr = data.get('track')
    if tr:
        lines.append('- 追踪实例 %s，单元 %s，类型 %s；%s，帧数 %s。实体来源 %s' %
                     (tr['instance'], tr['element'], tr.get('element_definition', {}).get('type', '未知'), tr['status'], len(tr['frames']), tr.get('element_definition', {}).get('source', '未知')))
        for frame in tr['frames']:
            geo = frame['geometry']; deformed = geo.get('deformed')
            lines.append('- 几何帧（坐标系一致性待核验）：%s 帧 %s，步时间 %s；状态 %s；采样最小缩放雅可比 %s；最短边 %s；节点 U 和 PEEQ 证据见 JSON。' %
                         (frame['step'], frame['frame_index'], frame['step_time'], geo.get('status'),
                          deformed.get('minimum_scaled_jacobian') if deformed else '未知', deformed.get('shortest_edge') if deformed else '未知'))
    links = data.get('element_model_links')
    if links:
        lines.append('- 单元与模型定义的关联结果及引用行号见 diagnosis_v3.json 的 element_model_links：' + json.dumps(links, ensure_ascii=False))
    surface_links = data.get('element_surface_links')
    if surface_links:
        lines.append('- 单元到表面及 Contact/Tie 的直接定义引用（不代表已发生接触）见 JSON 的 element_surface_links：' + json.dumps(surface_links, ensure_ascii=False))
    for fact in data['assessment']['facts']:
        if fact['type'] == 'recorded_increment_ratio':
            lines.append('- 同步首条与最小采样的增量比：%.6g；分别来自 %s:%s 与 %s:%s。' %
                         (fact['ratio'], fact['first']['evidence']['file'], fact['first']['evidence']['line'],
                          fact['minimum']['evidence']['file'], fact['minimum']['evidence']['line']))
    if tr and not any(f.get('peeq') for f in tr['frames']):
        lines.append('- 该单元未提取到 PEEQ；不能判断塑性应变是否为零。')
    if tr and not any(f.get('contact') for f in tr['frames']):
        lines.append('- 单元节点未提取到接触场值；不代表不存在接触。')
    lines += ['', '## 二、规则与证据边界',
              '- 稳定时间增量为 STA 记录的采样值；关键单元是该记录的求解器标记，不等于已确认根因。',
              '- 能量比仅在同一区域、同一记录段且同一时间唯一配对；内能接近零时不计算。它不是焊接质量或模型可信度的单独判据。',
              '- 保存帧不能代表帧间峰值；网格指标只覆盖支持的单元类型和被追踪单元，不代表全模型。',
              '- ODB 二进制没有文本行号；通过提取 CSV 行号、步骤、帧、实例、节点/单元和积分点追溯。',
              '- 日志、INP 和 ODB 即使同名也可能来自不同运行；时间、哈希和版本一致性未确认时不能建立因果链。',
              '- 输入快照变化检查只检查文件大小和修改时间，未变化也不能证明源文件内容和作业版本一致。',
              '', '## 三、可能原因与优先验证（非自动结论）']
    for rule in data['assessment']['rules']:
        lines.append('- 程序规则：%s，步 %s 帧 %s 时间 %s。' % (rule['interpretation'], rule['step'], rule['frame_index'], rule['step_time']))
    for item in data['assessment']['hypotheses']:
        lines.append('- %s：%s；%s' % (item['status'], item['question'], item['reason']))
    for item in data['assessment']['recommendations']:
        lines.append('%s. %s 代价或限制：%s 验证：%s' % (item['priority'], item['action'], item['tradeoff'], item['verification']))
    lines += ['', '## 四、在 Abaqus/CAE 中复核',
              '1. 在 Visualization 打开与报告作业对应的 ODB。通过显示组按实例和单元标签筛选关键单元，并核实它实际所属的零件。',
              '2. 选择报告记录的分析步与保存帧，依次查看 U、PEEQ、S 及实际存在的 CPRESS/COPEN/CSLIP 等；检查积分点、接触面及坐标方向，不同变量可能位于不同位置。',
              '3. U 是节点相对参考位置的位移；PEEQ 是累积等效塑性应变，并非位移或焊接强度；S 是应力；CPRESS 是接触压力、COPEN 是接触间隙，具体符号和接触定义需结合软件输出核实。',
              '4. 在 XY 数据中分别绘制对应分析步的能量历程；ALLAE 表示人工应变能，ALLIE 为内能，ALLKE 为动能。比例要结合绝对值、载荷和计算过程判断，不凭单一阈值下结论。',
              '', '## 五、未解决及限制']
    lines += ['- ' + x for x in data['limitations']]
    lines.append('- 任何模型修改和重新计算都由用户决定并操作；本报告不执行求解。')
    return '\n'.join(lines) + '\n'
