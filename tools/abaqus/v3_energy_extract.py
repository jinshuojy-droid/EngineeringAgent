"""通过 Abaqus Python 只读导出 ODB 中实际存在的能量历程。"""
import argparse
import csv
import json
import math
import os
import sys

EXPECTED = ('ALLKE', 'ALLIE', 'ALLAE', 'ETOTAL', 'ALLWK', 'ALLSE')
PROJECT = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _under(path, root):
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root)
    except ValueError:  # 不同 Windows 盘符
        return False


def extract(odb_path, output, step_filter=None, open_odb=None, project_root=None):
    """返回清单；各样本独立记录，时间和变量不做对齐或插值。

    open_odb 与 project_root 参数用于注入测试环境；正常调用使用 Abaqus 官方 API。
    """
    project = os.path.realpath(project_root or PROJECT)
    source = os.path.realpath(os.path.abspath(odb_path))
    target = os.path.realpath(os.path.abspath(output))
    if not _under(target, project) or target == project:
        raise ValueError('输出必须是 EngineeringAgent 项目内的新目录')
    if _under(target, os.path.dirname(source)):
        raise ValueError('输出不能位于 ODB 所在目录或其子目录')
    if _under(target, os.path.join(project, 'versions')):
        raise ValueError('不能写入冻结版本目录')
    if os.path.exists(target):
        raise ValueError('输出目录已存在，拒绝覆盖')
    if open_odb is None:
        from odbAccess import openOdb
        open_odb = openOdb
    odb = open_odb(path=source, readOnly=True)
    try:
        selected = set(step_filter or [])
        missing_steps = selected - set(odb.steps.keys())
        if missing_steps:
            raise ValueError('找不到分析步: ' + ', '.join(sorted(missing_steps)))
        meta = {'schema_version': '3.0-energy', 'odb_path': source,
                'read_only': True, 'expected_variables': list(EXPECTED),
                'selection': sorted(selected), 'steps': [], 'warnings': [],
                'time_note': '各变量保持原始采样，不对齐或插值；时间含义以分析步 domain 为准。'}
        os.makedirs(target)
        csv_path = os.path.join(target, 'energy_history.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['odb_file', 'step', 'domain', 'history_region', 'variable',
                             'sample_index', 'segment', 'step_time', 'odb_total_time', 'value'])
            for step_name, step in odb.steps.items():
                if selected and step_name not in selected:
                    continue
                domain = str(getattr(step, 'domain', 'UNKNOWN'))
                start = _number(getattr(step, 'totalTime', None)) if domain == 'TIME' else None
                record = {'name': str(step_name), 'domain': domain, 'regions': {},
                          'variables_present': [], 'expected_missing': []}
                present = set()
                for region_name, region in step.historyRegions.items():
                    region_vars = {}
                    for name, history in region.historyOutputs.items():
                        name = str(name)
                        if not (name.upper().startswith('ALL') or name.upper() == 'ETOTAL'):
                            continue
                        present.add(name.upper())
                        count, segment, previous = 0, 0, None
                        for index, pair in enumerate(history.data):
                            t, val = _number(pair[0]), _number(pair[1])
                            if t is not None and previous is not None and t < previous:
                                segment += 1
                            if t is not None:
                                previous = t
                            if t is None or val is None:
                                meta['warnings'].append('非有限或无效数值: %s / %s / %s 样本 %d' %
                                                        (step_name, region_name, name, index))
                            total = start + t if start is not None and t is not None else None
                            writer.writerow([source, step_name, domain, region_name, name, index,
                                             segment, t, total, val])
                            count += 1
                        region_vars[name] = count
                    if region_vars:
                        record['regions'][str(region_name)] = region_vars
                record['variables_present'] = sorted(present)
                record['expected_missing'] = sorted(set(EXPECTED) - present)
                meta['steps'].append(record)
        with open(os.path.join(target, 'energy_manifest.json'), 'w', encoding='utf-8') as stream:
            json.dump(meta, stream, ensure_ascii=False, indent=2, allow_nan=False)
        return meta
    finally:
        odb.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读导出 ODB 能量历程')
    parser.add_argument('--odb', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--step', action='append')
    args = parser.parse_args(argv)
    try:
        extract(args.odb, args.output, args.step)
    except Exception as exc:
        print('能量提取失败: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
