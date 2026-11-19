"""EngineeringAgent V3 统一入口：日志、INP 与 ODB 导出只读诊断。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import result_mode
import log_analysis
import v3_diagnosis
from v3_tracks import element_track

PROJECT = Path(__file__).resolve().parents[2]


def parser():
    p = argparse.ArgumentParser(description='只读联合诊断；不启动、中止或修改作业')
    p.add_argument('--version', action='version', version='EngineeringAgent V3.6')
    p.add_argument('--directory', required=True, help='原作业日志所在目录')
    p.add_argument('--job', required=True, help='作业名称，不带扩展名')
    p.add_argument('--inp', help='明确指定 INP；未指定时检查同名 INP（存在时）')
    p.add_argument('--odb', help='指定 ODB 后使用 Abaqus Python 只读提取')
    p.add_argument('--extracted', help='复用已有提取目录；不能与 --odb 同时使用')
    p.add_argument('--energy-extracted', help='复用 V3 能量提取目录；仅与 --extracted 一起使用')
    p.add_argument('--abaqus', help='Abaqus 启动命令，仅 --odb 使用')
    p.add_argument('--instance', help='明确追踪的实例，必须与 --element 同时使用')
    p.add_argument('--element', type=int, help='明确追踪的单元标签')
    p.add_argument('--output', help='报告根目录，必须位于项目内；默认 reports/v3')
    return p


def execute(args):
    from v3_inp import parse_inp, locate_element
    from v3_geometry import assess_element
    from v3_links import link_element
    source = Path(args.directory).resolve()
    if not source.is_dir() or not args.job or Path(args.job).name != args.job or args.job in ('.', '..'):
        raise ValueError('必须提供有效输入目录和不含路径的作业名')
    if bool(args.instance) != (args.element is not None) or (args.element is not None and args.element <= 0):
        raise ValueError('实例与正数单元标签必须同时提供')
    if args.odb and args.extracted:
        raise ValueError('--odb 和 --extracted 不能同时使用')
    if args.energy_extracted and not args.extracted:
        raise ValueError('--energy-extracted 需要 --extracted')
    if args.abaqus and not args.odb:
        raise ValueError('--abaqus 仅可与 --odb 一起使用')
    output_root = Path(args.output).resolve() if args.output else PROJECT / 'reports' / 'v3'
    if not result_mode.within(output_root, PROJECT) or result_mode.within(output_root, source) or result_mode.within(output_root, PROJECT / 'versions'):
        raise ValueError('报告必须在项目内，且不得写入原始作业目录或冻结版本')
    candidates = [p for p in source.iterdir() if p.is_file() and p.stem.lower() == args.job.lower()
                  and p.suffix.lower() in ('.sta', '.msg', '.dat', '.log', '.lck')]
    before = v3_diagnosis.captured_inputs(candidates)
    logs = log_analysis.diagnose(source, args.job)
    inp = Path(args.inp).resolve() if args.inp else source / (args.job + '.inp')
    if args.inp and not inp.is_file():
        raise ValueError('指定的 INP 不存在：' + str(inp))
    inp = inp if inp.is_file() else None
    odb = Path(args.odb).resolve() if args.odb else None
    if odb and (not odb.is_file() or odb.suffix.lower() != '.odb'):
        raise ValueError('指定的 ODB 不存在或扩展名不为 .odb')
    if odb and (odb.parent != source or odb.stem.lower() != args.job.lower()):
        raise ValueError('指定 ODB 必须与作业日志同目录且同名；仍须人工核对运行版本')
    tracked_paths = [str(p) for p in candidates] + ([str(inp)] if inp else []) + ([str(odb)] if odb else [])
    before.update(v3_diagnosis.captured_inputs([p for p in tracked_paths if p not in before]))
    out = result_mode.new_output(output_root)
    extracted = Path(args.extracted).resolve() if args.extracted else None
    energy_dir = Path(args.energy_extracted).resolve() if args.energy_extracted else None
    if odb:
        command = args.abaqus or shutil.which('abaqus') or shutil.which('abaqus.bat')
        if not command:
            raise ValueError('找不到 Abaqus Python；输出目录：' + str(out))
        extracted = out / 'extracted'
        env = os.environ.copy(); env['PYTHONIOENCODING'] = 'utf-8'
        cmd = [str(command), 'python', str(PROJECT / 'tools' / 'abaqus' / 'odb_extract.py'), '--odb', str(odb), '--output', str(extracted)]
        proc = subprocess.run(cmd, cwd=str(out), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        (out / 'extractor.log').write_text('exit_code=%s\nstdout:\n%s\nstderr:\n%s' % (proc.returncode, proc.stdout, proc.stderr), encoding='utf-8')
        if proc.returncode or not (extracted / 'odb_inventory.json').is_file():
            raise ValueError('只读 ODB 提取失败：' + str(out / 'extractor.log'))
        energy_dir = out / 'energy_extracted'
        cmd = [str(command), 'python', str(PROJECT / 'tools' / 'abaqus' / 'v3_energy_extract.py'), '--odb', str(odb), '--output', str(energy_dir)]
        proc = subprocess.run(cmd, cwd=str(out), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        (out / 'energy_extractor.log').write_text('exit_code=%s\nstdout:\n%s\nstderr:\n%s' % (proc.returncode, proc.stdout, proc.stderr), encoding='utf-8')
        if proc.returncode or not (energy_dir / 'energy_manifest.json').is_file():
            raise ValueError('只读 ODB 能量提取失败：' + str(out / 'energy_extractor.log'))
    inventory = None
    if extracted:
        for name in ('odb_inventory.json', 'history.csv', 'field.csv', 'entities.csv'):
            if not (extracted / name).is_file():
                raise ValueError('提取目录缺少 ' + name)
        inventory = json.loads((extracted / 'odb_inventory.json').read_text(encoding='utf-8'))
        inventory_odb = Path(inventory['odb_path']).resolve()
        if inventory_odb.parent != source or inventory_odb.stem.lower() != args.job.lower():
            raise ValueError('提取清单中的 ODB 路径与作业目录或作业名不一致')
    energy_manifest = None
    if energy_dir:
        energy_manifest = json.loads((energy_dir / 'energy_manifest.json').read_text(encoding='utf-8'))
        if Path(energy_manifest['odb_path']).resolve() != inventory_odb:
            raise ValueError('能量提取清单与场数据清单的 ODB 路径不同')
        if not (energy_dir / 'energy_history.csv').is_file():
            raise ValueError('能量提取目录缺少 energy_history.csv')
    export_paths = ([extracted / name for name in ('odb_inventory.json', 'history.csv', 'field.csv', 'entities.csv')] if extracted else [])
    export_paths += ([energy_dir / name for name in ('energy_manifest.json', 'energy_history.csv')] if energy_dir else [])
    before.update(v3_diagnosis.captured_inputs(export_paths))
    tracked_paths += [str(p) for p in export_paths]
    model = parse_inp(inp) if inp else None
    critical = logs['stable_increment'].get('minimum_recorded')
    instance = args.instance or (critical.get('instance') if critical else None)
    element = args.element if args.element is not None else (critical.get('element') if critical else None)
    track = element_track(extracted, instance, element, assess_element, str(inventory_odb)) if extracted and instance and element else None
    energy = v3_diagnosis.energy_summary(extracted, energy_dir / 'energy_history.csv' if energy_dir else None, str(inventory_odb)) if extracted else []
    after = v3_diagnosis.captured_inputs(tracked_paths)
    changes = [p for p in before if before[p] != after[p]]
    data = {'schema_version': '3.0', 'agent_version': '3.1', 'job': args.job, 'input_directory': str(source),
            'inputs': {'inp': str(inp) if inp else None, 'odb': str(odb) if odb else (inventory['odb_path'] if inventory else None),
                       'extracted': str(extracted) if extracted else None, 'energy_extracted': str(energy_dir) if energy_dir else None},
            'input_file_stat_before': before, 'input_file_stat_after': after, 'changed_during_read': changes,
            'logs': logs, 'model': model, 'track': track, 'energy': energy, 'energy_manifest': energy_manifest,
            'assessment': v3_diagnosis.assess(logs, track),
            'element_model_links': locate_element(model, instance, element) if model and instance and element else None,
            'element_surface_links': link_element(model, instance, element) if model and instance and element else None,
            'selection': {'instance': instance, 'element': element,
                          'method': '用户指定' if args.instance else 'STA 中最小采样记录的关键单元'},
            'limitations': ['INP、日志及 ODB 仅按路径和名称关联，不能证明属于同一次运行。',
                            '记录若仍在变化，跨文件读取并非原子快照；状态与时间序列仅代表读取时可见内容。',
                            '不自动确认材料真实性、接触机理、焊接成功或物理可信度。',
                            '接触追踪仅覆盖关键单元节点上实际导出的接触变量，不证明接触面双方身份或接触机制。',
                            '场数据与能量数据由两次只读调用分别采集；同一路径不证明来自同一时刻或同一版本。',
                            'ODB 全量场输出会产生较大的 CSV/JSON 文件，可能消耗较多时间和磁盘空间。',
                            'ETOTAL 和能量分量必须依据当前分析物理过程与输出定义解释；未实现完整能量平衡结论。',
                            'U 用于坐标重建前须确认其坐标系与节点坐标一致；稀疏帧可能遗漏峰值。']}
    if changes:
        data['limitations'].append('读取期间文件大小或修改时间发生变化：' + ', '.join(changes))
    if not inp:
        data['limitations'].append('未找到 INP，模型拓扑和约束无法审计。')
    if not extracted:
        data['limitations'].append('未提供 ODB 或现有提取数据，无法执行几何与能量分析。')
    elif not energy_dir:
        data['limitations'].append('复用已有导出时 ALLWK 等可能未提取，无法完整评估能量平衡。')
    if not track:
        data['limitations'].append('未确认实例和单元，未执行网格几何追踪。')
    v3_diagnosis.write_track_csv(out / 'element_frames.csv', track)
    plots, plot_error = v3_diagnosis.plot_results(out, logs, track, energy)
    data['plots'] = plots
    if plot_error: data['limitations'].append(plot_error)
    (out / 'diagnosis_v3.json').write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    (out / '诊断报告.md').write_text(v3_diagnosis.report(data), encoding='utf-8')
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'mesh-compare':
        from v35_support import mesh_compare, export_mesh_compare
        p=argparse.ArgumentParser(description='比较已有 review 数据，不启动计算')
        p.add_argument('--reviews',nargs='+',required=True);p.add_argument('--step',required=True)
        p.add_argument('--region',required=True);p.add_argument('--variable',required=True)
        p.add_argument('--time',type=float,required=True);p.add_argument('--tolerance',type=float,required=True)
        p.add_argument('--output',required=True,help='项目目录内新 JSON 文件')
        a=p.parse_args(argv[1:])
        try:
            output=Path(a.output).resolve()
            if PROJECT not in output.parents or output.exists():raise ValueError('输出必须是项目内不存在的新文件')
            if a.tolerance < 0:raise ValueError('时间容差不能为负')
            result=mesh_compare(a.reviews,a.step,a.region,a.variable,a.time,a.tolerance)
            output.parent.mkdir(parents=True,exist_ok=True)
            result['outputs']=export_mesh_compare(result,output)
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print('对照数据：'+str(output));return 0
        except (OSError,ValueError,KeyError,json.JSONDecodeError) as exc:
            print('对照未完成：'+str(exc),file=sys.stderr);return 2
    if argv and argv[0] == 'config-register':
        from v35_support import register_config
        p=argparse.ArgumentParser(description='登记用户确认的输入文件与结果检查配置')
        p.add_argument('--inp',required=True);p.add_argument('--config',required=True)
        a=p.parse_args(argv[1:])
        try:
            print(json.dumps(register_config(PROJECT/'config'/'review_config_registry.json',a.inp,a.config),ensure_ascii=False,indent=2))
            return 0
        except (OSError,ValueError,json.JSONDecodeError) as exc:
            print('配置登记失败：'+str(exc),file=sys.stderr);return 2
    if argv and argv[0] == 'watch':
        from v35_support import watch
        p=argparse.ArgumentParser(description='只读查看 STA 进度，不操作作业')
        p.add_argument('--sta',required=True);p.add_argument('--step',required=True,type=int)
        p.add_argument('--target',required=True,type=float,help='当前分析步的计划持续时间')
        a=p.parse_args(argv[1:])
        if a.target <= 0:p.error('--target 必须大于零')
        try:
            print(json.dumps(watch(a.sta,a.step,a.target),ensure_ascii=False,indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print('进度读取失败：'+str(exc),file=sys.stderr);return 2
    if argv and argv[0] == 'review':
        from v32_review import main as review_main
        return review_main(argv[1:])
    if argv and argv[0] == 'target':
        try:
            from v31_target import main as target_main
            return target_main(argv[1:])
        except (OSError, ValueError, KeyError, ImportError, json.JSONDecodeError) as exc:
            print('局部分析失败：' + str(exc), file=sys.stderr)
            return 2
    if argv and argv[0] == 'study':
        try:
            from v31_study import main as study_main
            return study_main(argv[1:])
        except (OSError, ValueError, KeyError, ImportError, json.JSONDecodeError) as exc:
            print('多工况分析失败：' + str(exc), file=sys.stderr)
            return 2
    if argv and argv[0] == 'results':
        try:
            args = result_mode.arguments(argv[1:])
            if args.output and not result_mode.within(Path(args.output).resolve(), PROJECT):
                raise ValueError('结果分析输出必须位于项目内')
            if args.output and result_mode.within(Path(args.output).resolve(), PROJECT / 'versions'):
                raise ValueError('不能写入冻结版本')
            out = result_mode.execute(args)
            print('EngineeringAgent 结果报告目录：' + str(out))
            return 0
        except (OSError, ValueError, ImportError, json.JSONDecodeError) as exc:
            print('结果分析失败：' + str(exc), file=sys.stderr)
            return 2
    try:
        out = execute(parser().parse_args(argv))
        print('EngineeringAgent V3.1 报告目录：' + str(out))
        return 0
    except (OSError, ValueError, KeyError, ImportError, json.JSONDecodeError) as exc:
        print('V3 处理失败：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
