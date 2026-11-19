"""X1 统一命令入口。"""
import argparse
import json
import math
from pathlib import Path
import re
import sys
from x1.core import search, audit_sensor, repair_sensor, cae_operation
from x1.thermal import convert, audit, material_copy
from x1.review import make_review, export_review
from x1.journal import add as journal_add, show as journal_show
from x1.edit import material_write, interaction_edit, semantic_diff, excel_material_spec


def parse_plane(value):
    match = re.fullmatch(r'([XYZxyz])\s*=\s*(.+)', value)
    if not match:
        raise ValueError('平面格式应为 X=数值、Y=数值或 Z=数值。')
    coordinate = float(match[2])
    if not math.isfinite(coordinate):
        raise ValueError('平面坐标必须为有限数值。')
    return match[1].upper(), coordinate


def local_parameters(args):
    if args.elements or args.elements_file:
        raw = args.elements or Path(args.elements_file).read_text(encoding='utf-8-sig')
        selector = {'kind': 'elements', 'value': sorted({int(label) for label in re.split(r'[,\s]+', raw.strip())})}
    elif args.bbox:
        bounds = [float(number) for number in args.bbox.split(',')]
        if len(bounds) != 6 or not all(map(math.isfinite, bounds)) or any(a >= b for a, b in zip(bounds[:3], bounds[3:])):
            raise ValueError('坐标盒需提供 xmin,ymin,zmin,xmax,ymax,zmax，每轴上下限必须递增。')
        selector = {'kind': 'bbox', 'value': bounds}
    elif args.boundary_plane:
        selector = {'kind': 'plane', 'value': [('XYZ'.index(axis), offset) for axis, offset in map(parse_plane, args.boundary_plane)],
                    'width': args.band_width}
    else:
        kind = next(name for name in ('nset', 'elset', 'surface') if getattr(args, name))
        selector = {'kind': 'named', 'value': (kind, getattr(args, kind))}
    numbers = [*args.seed_size, args.band_width, args.bias, args.min_size, args.min_step, args.min_warning_reduction]
    if (not all(map(math.isfinite, numbers)) or any(size <= 0 for size in args.seed_size)
            or args.band_width <= 0 or args.bias < 1 or args.min_size < 0 or args.min_step < 0
            or not 0 <= args.min_warning_reduction <= 1):
        raise ValueError('布种、带宽、偏置或验收阈值超出有效范围。')
    if args.bias != 1 and not args.bias_toward:
        raise ValueError('偏置布种需要显式提供 --bias-toward。')
    bias_axis = ('XYZ'.index(args.bias_toward[0]), args.bias_toward[2:]) if args.bias_toward else None
    return selector, {'sizes': args.seed_size, 'preview': args.preview, 'resume': args.resume, 'algorithm': args.tet_algorithm,
                      'partition': parse_plane(args.partition_probe) if args.partition_probe else None,
                      'accept_expansion': args.accept_cell_expansion, 'bias': args.bias, 'bias_axis': bias_axis,
                      'min_size': args.min_size, 'min_step': args.min_step, 'min_warning_reduction': args.min_warning_reduction}


def main():
    parser = argparse.ArgumentParser(description='X1：Abaqus 前处理查询、检查及副本操作')
    sub = parser.add_subparsers(dest='cmd', required=True)
    parser.add_argument('--version', action='version', version='Abaqus_tutor X1.6')
    p = sub.add_parser('mesh-audit', help='只读核查 INP/DAT 网格警告并定位单元')
    p.add_argument('--inp', required=True)
    p.add_argument('--dat', required=True, help='明确指定对应作业的 DAT；关联关系仍需核对')
    p.add_argument('--part', help='只显示指定部件')
    p.add_argument('--output-dir')
    p = sub.add_parser('mesh-optimize', help='固定外边界，在 INP 副本中改善四面体内部节点')
    p.add_argument('--inp', required=True)
    p.add_argument('--dat', required=True)
    p.add_argument('--part', required=True)
    p.add_argument('--max-nodes', type=int, default=2000)
    p.add_argument('--passes', type=int, default=3)
    p.add_argument('--output-dir')
    p = sub.add_parser('mesh-compare', help='只读比较原始和候选 INP 的网格与输入变更')
    p.add_argument('--before', required=True)
    p.add_argument('--after', required=True)
    p.add_argument('--part', required=True)
    p = sub.add_parser('mesh-study', help='在独立 CAE 副本中进行受控网格对照，不运行作业')
    p.add_argument('--cae', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--part', required=True)
    p.add_argument('--abaqus', default='abaqus')
    p.add_argument('--output-dir')
    p = sub.add_parser('cae-mesh-optimize', help='在 CAE 副本中优化四面体内部节点并由 Abaqus 复核')
    p.add_argument('--cae', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--part', required=True)
    p.add_argument('--abaqus', default='abaqus')
    p.add_argument('--output-dir')
    p.add_argument('--max-nodes', type=int, default=2000)
    p.add_argument('--passes', type=int, default=10)
    p.add_argument('--baseline', help='可选，显式提供同一网格连接的基准导出 JSON')
    default_runs = Path(__file__).resolve().parent / 'runs'
    p = sub.add_parser('cae-mesh-locate', help='从 CAE 网格导出定位警告和外缘分带')
    p.add_argument('--export', required=True)
    p.add_argument('--boundary-plane', action='append', default=[])
    p.add_argument('--band-width', type=float, default=1)
    p.add_argument('--job', default='未启动作业')
    p.add_argument('--output-dir', default=default_runs)
    p = sub.add_parser('cae-mesh-local', help='预览选区或在 CAE 副本中仅重划指定实体区域')
    p.add_argument('--cae', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--part', required=True)
    p.add_argument('--abaqus', default='abaqus')
    p.add_argument('--output-dir', default=default_runs)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--elements')
    selection.add_argument('--elements-file')
    selection.add_argument('--bbox')
    selection.add_argument('--nset')
    selection.add_argument('--elset')
    selection.add_argument('--surface')
    selection.add_argument('--boundary-plane', action='append')
    p.add_argument('--band-width', type=float, default=1)
    p.add_argument('--seed-size', type=float, nargs='+', default=[])
    p.add_argument('--bias', type=float, default=1)
    p.add_argument('--bias-toward', choices=[f'{axis}={end}' for axis in 'XYZ' for end in ('low', 'high')])
    p.add_argument('--min-size', type=float, default=.05)
    p.add_argument('--min-step', type=float, default=0)
    p.add_argument('--min-warning-reduction', type=float, default=0)
    p.add_argument('--tet-algorithm', choices=('keep','default','legacy'), default='keep')
    p.add_argument('--accept-cell-expansion', action='store_true')
    p.add_argument('--partition-probe', help='测试单个分区平面对已有网格的影响')
    p.add_argument('--preview', action='store_true')
    p.add_argument('--resume', help='已有试验目录，其他参数应与首轮一致')
    p = sub.add_parser('ask', help='查询前处理操作')
    p.add_argument('question')
    p.add_argument('--limit', type=int, default=3)
    p.add_argument('--inp', help='可选：从已保存 INP 识别分析类型')
    p.add_argument('--mode', choices=('coupled','adiabatic','mechanical'), help='显式指定本次查询的分析类型')
    p = sub.add_parser('sensor-audit', help='只读检查 INP 传感器')
    p.add_argument('--inp', required=True)
    p = sub.add_parser('thermal-audit', help='只读检查 INP 热前处理')
    p.add_argument('--inp', required=True)
    p = sub.add_parser('thermal-review', help='只读综合检查并输出中文报告')
    p.add_argument('--inp', required=True)
    p.add_argument('--material-record', help='可选材料来源记录 JSON')
    p.add_argument('--unit-system', choices=('N-mm-s-tonne-C','N-mm-s-tonne-K'), help='仅用户确认后指定')
    p.add_argument('--output-dir', help='非原始作业目录的报告根目录')
    p = sub.add_parser('journal-add', help='记录明确说明或核对过的操作')
    p.add_argument('--name', required=True, help='记录名称，字母数字下划线或短横线')
    p.add_argument('--event', required=True)
    p.add_argument('--status', choices=('用户说明','输入文件已核对','待核对'), required=True)
    p.add_argument('--evidence', help='可选证据路径和行号')
    p.add_argument('--note', help='记录依据或限制')
    p = sub.add_parser('journal-show', help='读取已有操作记录')
    p.add_argument('--name', required=True)
    p = sub.add_parser('unit-convert', help='换算热物性到 N-mm-s-tonne-K')
    p.add_argument('--quantity', choices=('specific_heat','conductivity','density'), required=True)
    p.add_argument('--value', type=float, required=True)
    p.add_argument('--unit', required=True)
    p = sub.add_parser('thermal-material-copy', help='在 INP 副本中添加指定材料热属性')
    p.add_argument('--inp', required=True)
    p.add_argument('--material', required=True)
    p.add_argument('--specific-heat', type=float, required=True)
    p.add_argument('--inelastic-fraction', type=float, required=True)
    p.add_argument('--output-dir')
    p = sub.add_parser('thermal-material-write', help='按 JSON 在副本中更新指定材料属性')
    p.add_argument('--inp', required=True)
    p.add_argument('--spec', required=True)
    p.add_argument('--output-dir')
    p = sub.add_parser('interaction-edit', help='按 JSON 在副本中编辑指定接触属性')
    p.add_argument('--inp', required=True)
    p.add_argument('--spec', required=True)
    p.add_argument('--output-dir')
    p = sub.add_parser('material-excel-spec', help='按明确映射将 Excel 数值转为材料 JSON')
    p.add_argument('--xlsx', required=True)
    p.add_argument('--mapping', required=True)
    p.add_argument('--output-dir')
    p = sub.add_parser('inp-diff', help='比较输入文件的关键词卡片及原始差异')
    p.add_argument('--before', required=True)
    p.add_argument('--after', required=True)
    p = sub.add_parser('sensor-repair', help='在独立 INP 副本中注册接触传感器')
    p.add_argument('--inp', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--surface', required=True)
    p.add_argument('--steps', nargs='+', required=True)
    p.add_argument('--output-dir')
    p = sub.add_parser('cae-inspect', help='后台检查保存的 CAE 副本')
    p.add_argument('--cae', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--abaqus', default='abaqus')
    p.add_argument('--output-dir')
    p = sub.add_parser('cae-remesh', help='后台重划 CAE 副本指定零件的网格')
    p.add_argument('--cae', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--part', required=True)
    p.add_argument('--size', type=float, required=True)
    p.add_argument('--abaqus', default='abaqus')
    p.add_argument('--output-dir')
    args = parser.parse_args()
    try:
        if args.cmd == 'mesh-audit':
            from x1.mesh_audit import mesh_audit
            result = mesh_audit(args.inp, args.dat, args.part, args.output_dir)
        elif args.cmd == 'mesh-optimize':
            from x1.mesh_optimize import mesh_optimize
            result = mesh_optimize(args.inp,args.dat,args.part,args.max_nodes,args.output_dir,args.passes)
        elif args.cmd == 'mesh-compare':
            from x1.mesh_optimize import verify_trial
            result = verify_trial(args.before,args.after,args.part)
        elif args.cmd == 'mesh-study':
            from x1.mesh_study import mesh_study
            result = mesh_study(args.cae, args.model, args.part, args.abaqus, args.output_dir)
        elif args.cmd == 'cae-mesh-optimize':
            from x1.mesh_cae_optimize import cae_mesh_optimize
            result = cae_mesh_optimize(args.cae,args.model,args.part,args.abaqus,args.output_dir,args.max_nodes,args.passes,args.baseline)
        elif args.cmd == 'cae-mesh-locate':
            from x1.mesh_local import warning_report
            if not math.isfinite(args.band_width) or args.band_width <= 0:
                raise ValueError('带宽必须为有限正数。')
            bands = [('XYZ'.index(axis), offset) for axis, offset in map(parse_plane, args.boundary_plane)]
            result = warning_report(args.export, args.output_dir, bands, args.band_width, args.job)
        elif args.cmd == 'cae-mesh-local':
            from x1.mesh_local import run_local
            selector, options = local_parameters(args)
            result = run_local(args.cae, args.model, args.part, selector, options, args.abaqus, args.output_dir)
        elif args.cmd == 'ask':
            mode=args.mode
            mode_note=None
            if args.inp:
                modes={s['type'] for s in audit(args.inp)['steps']}
                if mode and (mode not in modes or 'unknown' in modes or len(modes)!=1):
                    mode_note='手动指定类型与输入文件存在不一致或其他未识别分析步；请核对。'
                elif not mode and len(modes)==1 and 'unknown' not in modes:
                    mode=next(iter(modes))
                elif not mode:
                    mode_note='输入文件包含多种或未识别分析类型；请指定具体分析步或 --mode，勿假设教程适用。'
            if not mode and not mode_note:mode_note='未指定分析类型；教程需结合当前分析步核对。'
            result = search(args.question, args.limit, mode)
            result['context_note']=mode_note
        elif args.cmd == 'unit-convert':
            result = convert(args.quantity,args.value,args.unit)
        elif args.cmd == 'thermal-audit':
            result = audit(args.inp)
        elif args.cmd == 'thermal-review':
            result = make_review(args.inp,args.material_record,args.unit_system)
            folder=export_review(result,args.output_dir)
            warnings=sum(f['status']=='warning' for f in result['findings'])
            unknown=sum(f['status']=='unknown' for f in result['findings'])
            log_warnings=sum(1 for log in result['logs']['files'] for item in log['items']
                             if re.search(r'\*\*\*\s*WARNING',item['text'],re.I))
            result={'ok':True,'version':'X1.4','input':result['input'],'report':str(folder/'报告.md'),
                    'json':str(folder/'结果.json'),'inputs_unchanged':result['source_sha256_before']==result['source_sha256_after'] and not result['files_changed'],
                    'model_rule_warnings':warnings,'unknown':unknown,'same_name_log_warning_records':log_warnings,
                    'note':'ok 表示报告已生成；同名日志仅供核对，不代表模型、作业或材料已通过验证。'}
        elif args.cmd == 'journal-add':
            result = journal_add(args.name,args.event,args.status,args.evidence,args.note)
        elif args.cmd == 'journal-show':
            result = journal_show(args.name)
        elif args.cmd == 'thermal-material-copy':
            result = material_copy(args.inp,args.output_dir,args.material,args.specific_heat,args.inelastic_fraction)
        elif args.cmd == 'thermal-material-write':
            result = material_write(args.inp,args.spec,args.output_dir)
        elif args.cmd == 'interaction-edit':
            result = interaction_edit(args.inp,args.spec,args.output_dir)
        elif args.cmd == 'material-excel-spec':
            result = excel_material_spec(args.xlsx,args.mapping,args.output_dir)
        elif args.cmd == 'inp-diff':
            result = semantic_diff(args.before,args.after)
        elif args.cmd == 'sensor-audit':
            result = audit_sensor(args.inp)
        elif args.cmd == 'sensor-repair':
            result = repair_sensor(args.inp, args.output_dir, args.name, args.surface, args.steps)
        else:
            if args.cmd == 'cae-remesh' and args.size <= 0:
                raise ValueError('网格尺寸必须大于零。')
            result = cae_operation(args.cae, args.output_dir,
                                   'remesh' if args.cmd == 'cae-remesh' else 'inspect',
                                   args.model, getattr(args, 'part', None), getattr(args, 'size', None), args.abaqus)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get('exit_code', 1 if result.get('ok') is False else 0)
    except ModuleNotFoundError as exc:
        print(json.dumps({'ok': False, 'error': f'当前 Python 解释器缺少 {exc.name}。'}, ensure_ascii=False), file=sys.stderr)
        return 4
    except (ValueError, OSError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
