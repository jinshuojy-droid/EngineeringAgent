"""系统 Python 图表、日志整合与中文报告。"""
import json
from collections import defaultdict
from pathlib import Path


def describe_rule(rule):
    """把机器可读规则转换为可追溯的中文说明。"""
    kind = rule['type']
    if kind == 'negative_contact_pressure':
        e = rule['evidence']
        return ('接触压力负值提示：%s 的第 %s 帧、%s，最小已保存分量 %.9g；'
                '来源 field.csv:%s。需核对输出定义与符号约定。' %
                (e['step'], e['frame_index'], e['variable'], e['min_component'], e['min_line']))
    if kind == 'user_energy_ratio_limit':
        return ('%s 的 ALLAE/ALLIE 最大已匹配值 %.9g；用户阈值 %.9g，%s。'
                '来源 history.csv:%s、%s；不构成可靠性判定。' %
                (rule['step'], rule['evidence']['ratio'], rule['limit'],
                 '超过' if rule['exceeded'] else '未超过',
                 rule['evidence']['allae']['csv_line'], rule['evidence']['allie']['csv_line']))
    if kind == 'user_preload_ke_ie_limit':
        return ('%s 的预压末 ALLKE/ALLIE 按用户阈值 %.9g 比较：%s；'
                '来源 history.csv:%s、%s。' % (rule['step'], rule['limit'],
                '超过' if rule['exceeded'] else '未超过', rule['allke']['csv_line'], rule['allie']['csv_line']))
    if kind == 'user_u3_range':
        e = rule['evidence']
        return ('%s 的 U3 已保存数据%s用户设定范围 [%s, %s]；'
                '来源 history.csv:%s、%s。' % (rule['step'], '超出' if rule['outside'] else '未超出',
                rule['min'], rule['max'], e['minimum']['csv_line'], e['maximum']['csv_line']))
    if kind == 'user_u3_direction':
        return ('%s 的 U3 最后记录段净变化 %.9g，%s用户设定的%s方向相反；'
                '来源 history.csv:%s、%s；不代表振动周期内每点方向。' %
                (rule['step'], rule['delta'], '与' if rule['opposite'] else '未与',
                 '正' if rule['expected'] == 'positive' else '负', rule['first']['csv_line'], rule['last']['csv_line']))
    return '%s：%s 条；%s' % (kind, rule.get('count', '未知'), rule.get('note', '需核对原始数据。'))


def write_plots(directory, groups, diagnosis, rp_region):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plots = []
    for step_index, step in enumerate(diagnosis['steps']):
        if diagnosis['steps'][step]['domain'] != 'TIME':
            continue
        for kind in ('displacement', 'energy', 'energy_ratio', 'CPRESS', 'COPEN', 'CSLIP', 'CDISP'):
            fig, ax = plt.subplots(figsize=(8, 4.2))
            drawn = False
            if kind in ('displacement', 'energy'):
                for (s, region, variable), rows in groups.items():
                    eligible = (kind == 'displacement' and region == rp_region and variable in ('U1','U2','U3')) or (
                        kind == 'energy' and region == 'Assembly ASSEMBLY' and variable in ('ALLKE','ALLIE','ALLAE'))
                    if s != step or not eligible:
                        continue
                    segments = sorted(set(r['segment'] for r in rows))
                    for seg in segments:
                        points = [r for r in rows if r['segment'] == seg and r['step_time'] is not None and r['value'] is not None]
                        if points:
                            ax.plot([r['step_time'] for r in points], [r['value'] for r in points],
                                    label=variable + (' seg %s' % seg if len(segments)>1 else ''), linewidth=1)
                            drawn = True
            elif kind == 'energy_ratio':
                samples = diagnosis['steps'][step]['energy_ratio']['samples']
                segments = sorted({x['allae']['segment'] for x in samples})
                for seg in segments:
                    points = [x for x in samples if x['allae']['segment'] == seg]
                    if points:
                        ax.plot([x['allae']['step_time'] for x in points], [x['ratio'] for x in points],
                                label='ALLAE/ALLIE seg %s' % seg, linewidth=1)
                        drawn = True
            else:
                by_var = defaultdict(list)
                for item in diagnosis['contact_frames']:
                    if item['step'] == step and item['variable'].upper().startswith(kind):
                        by_var[item['variable']].append(item)
                for variable, items in by_var.items():
                    items.sort(key=lambda x:x['frame_index'])
                    ax.plot([x['step_time'] for x in items], [x['max_component'] for x in items],
                            label=variable + ' max component', linewidth=1)
                    drawn = True
            if drawn:
                ax.set_xlabel('Step time (ODB units)')
                ax.set_ylabel({'displacement':'Displacement (ODB units)', 'energy':'Energy (ODB units)',
                               'energy_ratio':'ALLAE / ALLIE'}.get(kind, 'Field component (ODB units)'))
                ax.set_title(step + ' / ' + kind)
                ax.grid(alpha=.25)
                ax.legend(fontsize=7, loc='best')
                fig.tight_layout()
                safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in step)
                filename = '%02d_%s_%s.png' % (step_index + 1, safe, kind)
                fig.savefig(str(directory / filename), dpi=180)
                plots.append(filename)
            plt.close(fig)
    return plots


def report(meta, result, log_result, plots, output):
    lines = ['# EngineeringAgent 仿真结果报告', '',
        '输入 ODB：' + meta['odb_path'], '作业：' + Path(meta['odb_path']).stem,
        '输出目录：' + str(output), '参考点历史区域（运行时指定，物理身份需核对）：' + str(result['reference_point_selection'] or '未指定'),
        '单位：' + meta['units'], '']
    lines += ['## 1. 数据清单与客观结果']
    for s in meta['steps']:
        info = result['steps'].get(s['name'])
        lines.append('- 分析步 %s：类型 %s，域 %s；声明时长 %s；最后保存帧 %s；ODB 步起始总时间 %s；帧数 %s；%s。' % (
            s['name'], s['procedure'], s['domain'], s['declared_time_period'], s['last_saved_frame_value'],
            s['odb_step_start_total_time'], len(s['frames']), '已提取' if s['extracted'] else '仅列清单'))
        if not info:
            continue
        lines.append('  - 未发现的目标历史变量：' + (', '.join(info['missing_requested_history']) or '无'))
        if result['reference_point_selection']:
            missing_rp = [name for name, data in info['reference_displacement'].items() if data is None]
            lines.append('  - 指定参考点区域未提取到的目标历史变量：' + (', '.join(missing_rp) or '无'))
        lines.append('  - 未发现的目标场变量：' + (', '.join(info['missing_requested_fields']) or '无'))
        lines.append('  - 实际存在的场变量：' + ', '.join(info['available_fields']))
        if s['domain'] != 'TIME':
            lines.append('  - 此步不是时间域，不能将帧坐标当作时间。')
        if info.get('u3_recorded_decrease'):
            r = info['u3_recorded_decrease']
            a, b = r['earlier'], r['later']
            lines.append('  - 运行时指定区域 U3 同段记录最大下降 %.9g：步坐标 %.9g 的 %.9g → %.9g 的 %.9g，history.csv:%d、%d。仅为已保存数据。' % (
                r['drop'], a['step_time'], a['value'], b['step_time'], b['value'], a['csv_line'], b['csv_line']))
        ratio = info['energy_ratio']['last']
        if ratio:
            a, b = ratio['allae'], ratio['allie']
            lines.append('  - 最后同点 ALLAE/ALLIE = %.6g，ALLAE %.9g、ALLIE %.9g；步时间 %.9g，history.csv:%d、%d。' % (
                ratio['ratio'], a['value'], b['value'], a['step_time'], a['csv_line'], b['csv_line']))
        if info.get('preload_ALLKE'):
            a = info['preload_ALLKE']['last']
            lines.append('  - 运行时指定预压步最后保存的 ALLKE %.9g，步时间 %.9g，history.csv:%d。' % (a['value'], a['step_time'], a['csv_line']))
        if info.get('preload_ke_ie'):
            r = info['preload_ke_ie']
            lines.append('  - 预压末同点 ALLKE/ALLIE = %.9g；history.csv:%d、%d；不等于自动判定是否准静态。' % (r['ratio'], r['allke']['csv_line'], r['allie']['csv_line']))
    lines += ['', '## 2. 程序规则判断']
    lines += ['- ' + describe_rule(r) for r in result['rules']] or ['- 未设置足以判定物理异常的工程阈值；请结合数据和工况核对。']
    lines += ['', '## 3. 尚需工程验证']
    lines += ['- ' + q for q in result['engineering_questions']]
    lines += ['', '## 4. 作业日志与限制']
    if log_result.get('status'):
        lines.append('- 日志状态：' + log_result['status']['value'])
        for e in log_result['status']['evidence']:
            lines.append('  - %s:%s %s' % (e['file'], e['line'], e['text']))
        lines.append('- 日志记录警告/错误 %d 条；完整列表和来源见 log_diagnosis.json。' % len(log_result['diagnostics']))
        lines.append('- 缺失日志：' + ', '.join(log_result['missing_files']))
    else:
        lines.append('- 作业日志不可用：' + log_result.get('error', '未知'))
    lines += ['', '## 5. 输出及使用边界',
        '- `extracted/odb_inventory.json`：步、帧、变量、实例及集合清单；`entities.csv`：节点和单元。',
        '- `extracted/history.csv` 和 `field.csv` 保留 ODB 路径、步骤、区域、时间、位置及标签；CSV 行号从表头开始计数。',
        '- `energy_ratio.csv` 只包含同点有效能量比及其原始历程行号；比值不代表可靠性的自动判定。',
        '- 接触图是每个已保存帧中对应变量各分量的最大值，不能解释为固定位置的连续历史。',
        '- 不同分析步分别绘图；总时间取 ODB step.totalTime + 帧/历程局部坐标，仅用于时间域。回退时按原始段拆分，不假设代表真实连续记录。',
        '- 声明时长和最后保存帧分开列示；末帧不必然就是实际终止时间。未输出变量是未知而非零。',
        '- 能量比只对同区域、同段且时间唯一匹配的取样；不插值。参考点由用户指定，不能仅凭节点编号认定。',
        '- ODB 为二进制，没有文件行号；以步、区域、样本/帧和 CSV 行号追溯。单位需结合模型核实。',
        '- 计算完成或单项指标均不能证明工艺目标达到或模型可信。', '', '## 曲线']
    lines += ['- [%s](%s)' % (p, p) for p in plots] if plots else ['- 没有满足条件的可绘制曲线。']
    return '\n'.join(lines) + '\n'
