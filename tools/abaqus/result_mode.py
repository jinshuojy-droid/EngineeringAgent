"""EngineeringAgent 内部结果分析模块：只读处理作业日志或 ODB。"""
import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import log_analysis
from history_analysis import read_history, field_summaries, build_diagnosis
from result_report import write_plots, report

PROJECT = Path(__file__).resolve().parents[2]


def within(path, root):
    return path == root or root in path.parents


def arguments(argv=None):
    p = argparse.ArgumentParser(description='只读检查 Abaqus 日志或 ODB；不启动计算')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--odb', help='ODB 文件的绝对或相对路径；同时检查同目录日志')
    source.add_argument('--directory', help='仅检查日志时的作业目录')
    p.add_argument('--job', help='仅检查日志时的作业名，不包含扩展名')
    p.add_argument('--abaqus', help='abaqus 命令或其绝对路径；默认 PATH')
    p.add_argument('--step', action='append', help='选择分析步，可重复；不选则全部')
    p.add_argument('--history-region', action='append', help='额外提取的历史区域，可重复；能量保留')
    p.add_argument('--rp-history-region', help='用户确认的参考点历史区域，例如 Node ASSEMBLY.1')
    p.add_argument('--preload-step', help='用户确认的预压分析步名称')
    p.add_argument('--u3-min', type=float, help='用户提供的 U3 下限；单位必须与 ODB 一致')
    p.add_argument('--u3-max', type=float, help='用户提供的 U3 上限；单位必须与 ODB 一致')
    p.add_argument('--u3-direction', choices=('positive', 'negative'), help='用户确认的 U3 净位移预期方向')
    p.add_argument('--energy-ratio-limit', type=float, help='用户指定的 ALLAE/ALLIE 提示阈值')
    p.add_argument('--preload-ke-ie-limit', type=float, help='用户指定的预压末 ALLKE/ALLIE 提示阈值')
    p.add_argument('--output', help='输出根目录；默认 reports/v3/results')
    return p.parse_args(argv)


def make_output_root(requested, source_dir, project_only=True):
    outroot = Path(requested).resolve() if requested else PROJECT / 'reports' / 'v3' / 'results'
    protected = [(PROJECT / 'versions').resolve()]
    if (project_only and not within(outroot, PROJECT)) or within(outroot, source_dir) or any(within(outroot, p) for p in protected):
        raise ValueError('输出目录不允许：不能位于原始作业目录或冻结版本内；ODB 输出必须在项目内')
    return outroot


def new_output(outroot):
    outroot.mkdir(parents=True, exist_ok=True)
    out = outroot / (datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8])
    out.mkdir()
    return out


def execute(args):
    if args.directory is not None:
        if not args.directory.strip():
            raise ValueError('作业目录不能为空')
        if not args.job:
            raise ValueError('仅检查日志时必须提供 --job')
        if any((args.abaqus, args.step, args.history_region, args.rp_history_region, args.preload_step,
                args.u3_min is not None, args.u3_max is not None, args.u3_direction,
                args.energy_ratio_limit is not None, args.preload_ke_ie_limit is not None)):
            raise ValueError('仅检查日志时不能使用 ODB 专属选项')
        result = log_analysis.diagnose(args.directory, args.job)
        outroot = make_output_root(args.output, Path(result['input_directory']), project_only=False)
        out = new_output(outroot)
        (out / 'log_diagnosis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (out / '诊断报告.md').write_text(log_analysis.report_text(result), encoding='utf-8')
        return out
    if args.job:
        raise ValueError('--job 仅用于 --directory 日志模式；ODB 模式自动使用 ODB 文件名')
    odb = Path(args.odb).resolve()
    if not odb.is_file() or odb.suffix.lower() != '.odb':
        raise ValueError('ODB 文件不存在或扩展名不为 .odb')
    if args.u3_min is not None and args.u3_max is not None and args.u3_min > args.u3_max:
        raise ValueError('U3 下限不能大于上限')
    if any(x is not None and not math.isfinite(x) for x in (args.u3_min, args.u3_max, args.energy_ratio_limit, args.preload_ke_ie_limit)):
        raise ValueError('范围与阈值必须是有限数值')
    if any(x is not None and x < 0 for x in (args.energy_ratio_limit, args.preload_ke_ie_limit)):
        raise ValueError('能量比阈值不能为负')
    if args.preload_ke_ie_limit is not None and not args.preload_step:
        raise ValueError('指定预压动能阈值时必须明确指定预压步')
    if not args.rp_history_region and (args.u3_min is not None or args.u3_max is not None or args.u3_direction):
        raise ValueError('指定 U3 范围或方向时必须明确指定参考点历史区域')
    outroot = make_output_root(args.output, odb.parent)
    command = args.abaqus or shutil.which('abaqus') or shutil.which('abaqus.bat')
    if not command:
        raise ValueError('找不到 Abaqus 命令，请通过 --abaqus 指定本机命令路径')
    if args.history_region and args.rp_history_region and args.rp_history_region not in args.history_region:
        args.history_region.append(args.rp_history_region)
    out = new_output(outroot)
    extracted = out / 'extracted'
    cmd = [str(command), 'python', str(PROJECT / 'tools' / 'abaqus' / 'odb_extract.py'),
           '--odb', str(odb), '--output', str(extracted)]
    for name in args.step or []:
        cmd.extend(['--step', name])
    for name in args.history_region or []:
        cmd.extend(['--history-region', name])
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        completed = subprocess.run(cmd, cwd=str(out), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    except OSError as exc:
        (out / '提取失败.txt').write_text(str(exc), encoding='utf-8')
        raise ValueError('无法启动 Abaqus Python，记录目录：%s；%s' % (out, exc)) from exc
    (out / 'extractor.log').write_text('exit_code=%s\nstdout:\n%s\nstderr:\n%s' % (
        completed.returncode, completed.stdout, completed.stderr), encoding='utf-8')
    inventory = extracted / 'odb_inventory.json'
    if completed.returncode != 0 or not inventory.is_file():
        raise ValueError('ODB 提取未完成，详见 %s' % (out / 'extractor.log'))
    meta = json.loads(inventory.read_text(encoding='utf-8'))
    if args.rp_history_region and not any(args.rp_history_region in s['history_regions'] for s in meta['steps'] if s['extracted']):
        raise ValueError('指定的参考点历史区域在所选分析步中不存在；清单：%s' % inventory)
    if args.preload_step and not any(s['name'] == args.preload_step and s['extracted'] for s in meta['steps']):
        raise ValueError('指定预压步不存在或未选取；清单：%s' % inventory)
    groups = read_history(extracted / 'history.csv')
    contacts, invalid = field_summaries(extracted / 'field.csv')
    result = build_diagnosis(meta, groups, contacts, invalid, args.rp_history_region,
                             args.preload_step, args.u3_min, args.u3_max, args.u3_direction,
                             args.energy_ratio_limit, args.preload_ke_ie_limit)
    try:
        logs = log_analysis.diagnose(odb.parent, odb.stem)
    except (OSError, ValueError) as exc:
        logs = {'error': str(exc), 'input_directory': str(odb.parent), 'job': odb.stem}
    (out / 'log_diagnosis.json').write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding='utf-8')
    with (out / 'energy_ratio.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['odb_file','step','region','segment','sample_index','step_time','odb_total_time',
                         'ALLAE','ALLIE','ratio','history_csv_line_ALLAE','history_csv_line_ALLIE'])
        for step, details in result['steps'].items():
            for point in details['energy_ratio']['samples']:
                a, b = point['allae'], point['allie']
                writer.writerow([str(odb),step,a['history_region'],a['segment'],a['sample_index'],a['step_time'],
                                 a['odb_total_time'],a['value'],b['value'],point['ratio'],a['csv_line'],b['csv_line']])
    plots = write_plots(out, groups, result, args.rp_history_region)
    result['plots'] = plots
    result['odb_file'] = str(odb)
    result['job'] = odb.stem
    result['input_directory'] = str(odb.parent)
    result['limitations'] = ['ODB 二进制无行号，以步骤、区域、样本索引和 CSV 行号追溯',
        '保存帧不一定是终止帧，结果采样不保证全程极值', '时间回退分段保留，不能自动判定重启和完整性',
        '未指定参考点不猜测；未指定单位、方向及范围不作异常断言', '不判断焊接成功或模型可信']
    (out / 'result_diagnosis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    (out / '仿真分析报告.md').write_text(report(meta, result, logs, plots, out), encoding='utf-8')
    return out


def main(argv=None):
    try:
        output = execute(arguments(argv))
        print('EngineeringAgent 结果报告目录：' + str(output))
        return 0
    except (OSError, ValueError, ImportError, json.JSONDecodeError) as exc:
        print('结果分析失败：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
