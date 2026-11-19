"""通用 INP 卡片对照与受控副本编辑；模型数值全部由调用者提供。"""
import difflib
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .core import ROOT, digest, new_run


def _cards(text):
    """建立关键词卡片及其上下文索引；保留原始行以便定位。"""
    lines = text.splitlines(keepends=True)
    cards = []
    scope = ''
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith('*') or s.startswith('**'):
            continue
        pieces = [x.strip() for x in s[1:].split(',')]
        key = pieces[0].lower()
        params = {}
        for p in pieces[1:]:
            if p:
                k, eq, v = p.partition('=')
                params[k.strip().lower()] = v.strip().strip('"\'') if eq else True
        if cards:
            cards[-1]['end'] = i
        if (scope.startswith('material:') or scope.startswith('surface interaction:')) and key in (
                'physical constants','boundary','initial conditions','amplitude','section controls',
                'contact','contact pair','tie','rigid body','output'):
            scope = ''
        if key in ('material', 'surface interaction', 'part', 'step', 'assembly'):
            scope = key + ':' + str(params.get('name', '?'))
        cards.append({'keyword': key, 'params': params, 'start': i, 'end': len(lines), 'scope': scope})
        if key in ('end part', 'end assembly', 'end step'):
            scope = ''
    return lines, cards


def _token(value):
    val = value.strip()
    try:
        d = Decimal(val.replace('D', 'E').replace('d', 'e'))
        if d.is_finite(): return str(d.normalize())
    except InvalidOperation:
        pass
    return val


def _canon(card, lines):
    data = []
    for line in lines[card['start'] + 1:card['end']]:
        line = line.strip()
        if line and not line.startswith('**'):
            values = [_token(x) for x in line.split(',')]
            if card['keyword'] in ('density','elastic','conductivity','specific heat','expansion',
                    'plastic','rate dependent','inelastic heat fraction','friction','gap heat generation','gap conductance'):
                while values and not values[-1]: values.pop()
            data.append(tuple(values))
    return (card['keyword'], tuple(sorted((k, _token(str(v)).lower()) for k, v in card['params'].items())), tuple(data))


def _diff_text(before, after):
    a, ca = _cards(before)
    b, cb = _cards(after)
    ac = [_canon(c, a) for c in ca]
    bc = [_canon(c, b) for c in cb]
    changes = []
    for tag, i, j, k, l in difflib.SequenceMatcher(None, ac, bc, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        def desc(cards, start, end):
            return [{'keyword': c['keyword'], 'scope': c['scope'], 'line': c['start'] + 1}
                    for c in cards[start:end]]
        change = {'change': tag, 'before': desc(ca, i, j), 'after': desc(cb, k, l)}
        if j-i == 1 and l-k == 1 and ca[i]['keyword'] == cb[k]['keyword'] and ca[i]['scope'].casefold() == cb[k]['scope'].casefold():
            old, new = ac[i][2], bc[k][2]
            fields = {'gap heat generation':['heat_conversion','secondary_fraction'], 'friction':['friction']}.get(ca[i]['keyword'],[])
            if len(old) == len(new) == 1:
                cells=[]; x=old[0]; y=new[0]
                for col in range(max(len(x),len(y))):
                    v1=x[col] if col<len(x) else None; v2=y[col] if col<len(y) else None
                    if v1 != v2:
                        cells.append({'row':1,'column':col+1,'field':fields[col] if col<len(fields) else None,
                                      'before':v1,'after':v2})
                if len(cells) <= 100: change['data_changes'] = cells
            elif max(len(old),len(new)) <= 1000:
                row_changes=[]
                for op, a1,a2,b1,b2 in difflib.SequenceMatcher(None,old,new,autojunk=False).get_opcodes():
                    if op != 'equal': row_changes.append({'change':op,'before':old[a1:a2],'after':new[b1:b2]})
                change['row_changes']=row_changes
        changes.append(change)
    includes = any(c['keyword'] == 'include' for c in ca + cb)
    return {'card_changes': changes, 'card_semantics_equal': not changes,
            'raw_bytes_equal': before == after, 'includes_present': includes,
            'complete': not includes,
            'note': '关键词、参数和数据行对比；忽略注释和部分格式，原始文本另行对照。INCLUDE 文件未展开。'}


def semantic_diff(before, after):
    a, b = Path(before).resolve(), Path(after).resolve()
    ta, tb = a.read_bytes().decode('latin-1'), b.read_bytes().decode('latin-1')
    result = _diff_text(ta, tb)
    result.update({'before': str(a), 'after': str(b), 'before_sha256': digest(a), 'after_sha256': digest(b)})
    return result


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('材料或接触参数必须为有限数字。')
    return str(value) if isinstance(value, int) else repr(value)


def _table(obj, lengths, *, increasing=False, positive_first=False, fraction=False, allow_multiple_base_rows=False):
    if not isinstance(obj, dict) or not isinstance(obj.get('rows'), list) or not obj['rows']:
        raise ValueError('属性须提供非空 rows 数值表。')
    rows = obj['rows']
    size = len(rows[0]) if isinstance(rows[0], list) else 0
    if size not in lengths or any(not isinstance(row, list) or len(row) != size for row in rows):
        raise ValueError('属性数据列数不一致或不受支持。')
    if len(lengths) > 1 and size == min(lengths) and len(rows) > 1 and not allow_multiple_base_rows:
        raise ValueError('常数属性只允许一行；温度表必须包含温度列。')
    for row in rows:
        for v in row: _number(v)
        if positive_first and row[0] <= 0: raise ValueError('属性第一列必须为正。')
        if fraction and not 0 <= row[0] <= 1: raise ValueError('比例须在 0 到 1 之间。')
    if increasing and size == max(lengths) and any(rows[i][-1] >= rows[i+1][-1] for i in range(len(rows)-1)):
        raise ValueError('温度列必须严格递增。')
    return ''.join(', '.join(_number(v) for v in row) + '\n' for row in rows)


def _spec(path):
    p = Path(path).resolve()
    value = json.loads(p.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict): raise ValueError('JSON 顶层必须是对象。')
    if not isinstance(value.get('unit_system'), str) or not value['unit_system'].strip():
        raise ValueError('必须明确提供 unit_system，数值须已按模型单位换算。')
    if not isinstance(value.get('source'), str) or not value['source'].strip():
        raise ValueError('必须提供参数 source。')
    return value, p


def _target(lines, cards, kind, name):
    found = [i for i, c in enumerate(cards) if c['keyword'] == kind
             and str(c['params'].get('name', '')).casefold() == name.casefold()]
    if len(found) != 1: raise ValueError('目标名称不存在或不唯一：' + name)
    first = found[0]
    endings = {'material', 'surface interaction', 'step', 'assembly', 'part', 'amplitude',
               'physical constants', 'boundary', 'initial conditions', 'section controls', 'elset', 'nset'}
    end = next((j for j in range(first + 1, len(cards)) if cards[j]['keyword'] in endings), len(cards))
    return first, end


def _edit_card(lines, cards, start, end, keyword, content):
    matches = [c for c in cards[start + 1:end] if c['keyword'] == keyword]
    if len(matches) > 1: raise ValueError('目标属性存在多张同名卡，需人工区分：' + keyword)
    if matches:
        c = matches[0]
        tail = lines[c['start']+1:c['end']]
        data_indices = [i for i, s in enumerate(tail) if s.strip() and not s.lstrip().startswith('**')]
        if data_indices and any(s.lstrip().startswith('**') for s in tail[:data_indices[-1]]):
            raise ValueError('目标卡片数据中间有注释，请先人工核对。')
        suffix = tail[data_indices[-1]+1:] if data_indices else tail
        lines[c['start']:c['end']] = [content] + suffix
    else:
        at = cards[start]['end']
        lines[at:at] = [content]
    return lines


def _save(source, spec_path, output_dir, kind, original, edited, details):
    root = Path(output_dir).expanduser().resolve() if output_dir else ROOT / 'runs'
    if root == source.parent or source.parent in root.parents:
        raise ValueError('输出目录不能位于原始作业目录及其子目录。')
    if source.read_bytes().decode('latin-1') != original:
        raise ValueError('处理期间输入发生变化，已取消写入。')
    card_type, target_name = ('material',details['material']) if kind == 'material_write' else ('surface interaction',details['interaction'])
    def outside(text):
        ls, cs = _cards(text); first, end = _target(ls, cs, card_type, target_name)
        stop = cs[end]['start'] if end < len(cs) else len(ls)
        return ''.join(ls[:cs[first]['start']] + ls[stop:]).encode('latin-1')
    untouched_before, untouched_after = outside(original), outside(edited)
    if untouched_before != untouched_after: raise ValueError('目标对象外内容发生变化，已取消写入。')
    folder = new_run(root, kind)
    target = folder / source.name
    target.write_bytes(edited.encode('latin-1'))
    difference = _diff_text(original, edited)
    (folder / '修改对照.diff').write_text(''.join(difflib.unified_diff(original.splitlines(keepends=True),
        edited.splitlines(keepends=True), fromfile=str(source), tofile=str(target))), encoding='utf-8')
    source_before = hashlib.sha256(original.encode('latin-1')).hexdigest()
    source_after = digest(source)
    result = {'ok': source_before == source_after, 'input': str(source), 'spec': str(spec_path), 'output': str(target),
              'source_sha256': source_before, 'source_sha256_after': source_after,
              'source_unchanged': source_before == source_after,
              'spec_sha256': digest(spec_path), 'output_sha256': digest(target),
              'outside_target_identical': True, 'outside_target_sha256': hashlib.sha256(untouched_before).hexdigest(),
              'semantic_diff': difference, 'details': details,
              'note': '仅生成独立 INP 副本；未修改 CAE，未运行数据检查或计算。'}
    (folder / '结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / '报告.md').write_text('# INP 副本修改\n\n输入：`%s`\n\n输出：`%s`\n\n参数文件：`%s`\n\n'
        '修改范围与卡片位置见结果.json，逐行变化见修改对照.diff。原始模型未修改。\n' % (source, target, spec_path), encoding='utf-8')
    return result


def material_write(path, spec_path, output_dir=None):
    spec, sp = _spec(spec_path)
    name, props = spec.get('material'), spec.get('properties')
    if not isinstance(name, str) or not name.strip() or not isinstance(props, dict) or not props:
        raise ValueError('请提供 material 和非空 properties。')
    supported = {'density', 'elastic', 'conductivity', 'specific_heat', 'expansion',
                 'plastic_johnson_cook', 'rate_johnson_cook', 'inelastic_heat_fraction'}
    if set(props) - supported: raise ValueError('包含不支持的材料属性：' + ', '.join(sorted(set(props)-supported)))
    source = Path(path).resolve(); original = source.read_bytes().decode('latin-1')
    lines, cards = _cards(original)
    if any(c['keyword'] == 'include' for c in cards): raise ValueError('副本编辑暂不支持 INCLUDE。')
    for key, obj in props.items():
        lines, cards = _cards(''.join(lines)); first, end = _target(lines, cards, 'material', name)
        if not isinstance(obj, dict) or set(obj) - ({'rows','zero'} if key == 'expansion' else {'rows'}):
            raise ValueError('材料属性含不支持的选项：' + key)
        if key == 'density':
            keyword, header = 'density', '*Density\n'; body = _table(obj, (1, 2), increasing=True, positive_first=True)
        elif key == 'elastic':
            keyword, header = 'elastic', '*Elastic\n'; body = _table(obj, (2, 3), increasing=True, positive_first=True)
            if any(not -1 < row[1] < .5 for row in obj['rows']): raise ValueError('各向同性泊松比必须位于 -1 与 0.5 之间。')
        elif key in ('conductivity', 'specific_heat'):
            keyword = key.replace('_', ' '); header = '*' + keyword.title() + '\n'
            body = _table(obj, (1, 2), increasing=True, positive_first=True)
        elif key == 'expansion':
            keyword = 'expansion'; header = '*Expansion'
            if 'zero' in obj: header += ', zero=' + _number(obj['zero'])
            header += '\n'; body = _table(obj, (1, 2), increasing=True)
        elif key == 'plastic_johnson_cook':
            keyword, header = 'plastic', '*Plastic, hardening=JOHNSON COOK\n'; body = _table(obj, (6,))
            if len(obj['rows']) != 1: raise ValueError('Johnson-Cook 塑性只支持单行六参数。')
            if any(v < 0 for v in obj['rows'][0][:4]) or obj['rows'][0][4] <= obj['rows'][0][5]:
                raise ValueError('Johnson-Cook 强度参数须非负，熔化温度须高于转变温度。')
        elif key == 'rate_johnson_cook':
            keyword, header = 'rate dependent', '*Rate Dependent, type=JOHNSON COOK\n'; body = _table(obj, (2,))
            if len(obj['rows']) != 1: raise ValueError('Johnson-Cook 应变率只支持单行两个参数。')
            if obj['rows'][0][0] < 0 or obj['rows'][0][1] <= 0: raise ValueError('应变率系数须非负，参考应变率须为正。')
        else:
            keyword, header = 'inelastic heat fraction', '*Inelastic Heat Fraction\n'; body = _table(obj, (1,), fraction=True)
            if len(obj['rows']) != 1: raise ValueError('非弹性转热比例只支持单个值。')
        existing = [c for c in cards[first+1:end] if c['keyword'] == keyword]
        if existing:
            p = existing[0]['params']
            if key == 'plastic_johnson_cook' and str(p.get('hardening','')).upper() != 'JOHNSON COOK': raise ValueError('现有塑性模型类型不同，拒绝覆盖。')
            if key == 'rate_johnson_cook' and str(p.get('type','')).upper() != 'JOHNSON COOK': raise ValueError('现有应变率模型类型不同，拒绝覆盖。')
            if key == 'plastic_johnson_cook' and set(p) != {'hardening'}: raise ValueError('现有塑性卡有附加选项，拒绝覆盖。')
            if key == 'rate_johnson_cook' and set(p) != {'type'}: raise ValueError('现有应变率卡有附加选项，拒绝覆盖。')
            if key in ('density', 'elastic', 'conductivity', 'specific_heat', 'expansion', 'inelastic_heat_fraction') and any(k not in ({'zero'} if key == 'expansion' else set()) for k in p):
                raise ValueError('现有属性有附加选项，拒绝自动覆盖：' + keyword)
        _edit_card(lines, cards, first, end, keyword, header + body)
    return _save(source, sp, output_dir, 'material_write', original, ''.join(lines),
                 {'material': name, 'properties': list(props), 'unit_system': spec['unit_system'], 'source': spec['source']})


def excel_material_spec(workbook, mapping_path, output_dir=None):
    """按用户显式标定的工作表和区域转换，不猜测表头、单位或参数。"""
    try:
        import openpyxl
        from openpyxl.utils.cell import range_boundaries
    except ImportError:
        raise ValueError('Excel 转换需要安装 openpyxl。')
    mapping, mp = _spec(mapping_path)
    source = Path(workbook).resolve()
    name, props = mapping.get('material'), mapping.get('properties')
    if not isinstance(name, str) or not name.strip() or not isinstance(props, dict) or not props:
        raise ValueError('映射需提供 material 和非空 properties。')
    workbook_hash = digest(source); book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    result_props = {}
    try:
        for key, item in props.items():
            if not isinstance(item, dict) or not isinstance(item.get('sheet'),str) or not isinstance(item.get('range'),str):
                raise ValueError('每项映射须明确 sheet 和 range。')
            if set(item) - {'sheet','range','zero'}: raise ValueError('存在不支持的 Excel 映射选项。')
            if item['sheet'] not in book.sheetnames: raise ValueError('找不到工作表：'+item['sheet'])
            try: c1,r1,c2,r2 = range_boundaries(item['range'])
            except ValueError: raise ValueError('Excel 数据区域无效：'+item['range'])
            if None in (c1,r1,c2,r2) or (c2-c1+1)*(r2-r1+1)>100000:
                raise ValueError('Excel 数据区域必须为有限的单元格区域。')
            rows = [list(row) for row in book[item['sheet']].iter_rows(min_row=r1,max_row=r2,min_col=c1,max_col=c2,values_only=True)]
            for row in rows:
                for value in row: _number(value)
            result_props[key] = {'rows':rows}
            if 'zero' in item: result_props[key]['zero'] = item['zero']
    finally: book.close()
    if digest(source) != workbook_hash: raise ValueError('读取期间 Excel 文件发生变化。')
    root = Path(output_dir).expanduser().resolve() if output_dir else ROOT/'runs'
    if root == source.parent or source.parent in root.parents: raise ValueError('输出目录不能位于原始表格目录及其子目录。')
    folder = new_run(root,'excel_material_spec')
    spec = {'material':name,'unit_system':mapping['unit_system'],'source':mapping['source'], 'properties':result_props}
    target=folder/'material_spec.json'; target.write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest={'ok':True,'input':str(source),'input_sha256':workbook_hash,'mapping':str(mp),
              'mapping_sha256':digest(mp),'output':str(target),'ranges':props,
              'note':'按指定区域读取已缓存数值；未自动推定单位或物理含义。'}
    (folder/'结果.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest


def interaction_edit(path, spec_path, output_dir=None):
    spec, sp = _spec(spec_path)
    name, changes = spec.get('interaction'), spec.get('changes')
    if not isinstance(name, str) or not name.strip() or not isinstance(changes, dict) or not changes:
        raise ValueError('请提供 interaction 和非空 changes。')
    allowed = {'friction', 'heat_conversion', 'secondary_fraction', 'gap_conductance'}
    if set(changes) - allowed: raise ValueError('包含不支持的接触参数。')
    source = Path(path).resolve(); original = source.read_bytes().decode('latin-1')
    lines, cards = _cards(original)
    if any(c['keyword'] == 'include' for c in cards): raise ValueError('副本编辑暂不支持 INCLUDE。')
    if 'friction' in changes:
        value = changes['friction']; _number(value)
        if value < 0: raise ValueError('摩擦系数须非负。')
        first, end = _target(lines, cards, 'surface interaction', name)
        matches = [c for c in cards[first+1:end] if c['keyword'] == 'friction']
        if len(matches) > 1 or (matches and matches[0]['params']): raise ValueError('仅支持无附加选项的常数摩擦卡。')
        if matches:
            rows = [x.strip() for x in lines[matches[0]['start']+1:matches[0]['end']] if x.strip() and not x.lstrip().startswith('**')]
            if len(rows) != 1 or len([x for x in rows[0].split(',') if x.strip()]) != 1: raise ValueError('现有摩擦非单值，拒绝覆盖。')
        _edit_card(lines, cards, first, end, 'friction', '*Friction\n' + _number(value) + ',\n')
    if 'heat_conversion' in changes or 'secondary_fraction' in changes:
        lines, cards = _cards(''.join(lines)); first, end = _target(lines, cards, 'surface interaction', name)
        matches = [c for c in cards[first+1:end] if c['keyword'] == 'gap heat generation']
        if len(matches) > 1 or (matches and matches[0]['params']): raise ValueError('仅支持标准生热卡。')
        if matches:
            c = matches[0]; rows = [s.strip() for s in lines[c['start']+1:c['end']] if s.strip() and not s.lstrip().startswith('**')]
            if len(rows) != 1: raise ValueError('生热卡数据行不唯一。')
            cols = rows[0].split(',')
        else:
            if not {'heat_conversion','secondary_fraction'} <= changes.keys(): raise ValueError('新增生热卡时必须同时明确转热和次表面分配比例。')
            cols = ['', '']
        if len(cols) < 2: cols.extend([''] * (2-len(cols)))
        for field, index in [('heat_conversion',0), ('secondary_fraction',1)]:
            if field in changes:
                value = changes[field]; _number(value)
                if not 0 <= value <= 1: raise ValueError('热量比例必须在 0 到 1 之间。')
                cols[index] = _number(value)
        _edit_card(lines, cards, first, end, 'gap heat generation', '*Gap Heat Generation\n' + ', '.join(x.strip() for x in cols) + '\n')
    if 'gap_conductance' in changes:
        lines, cards = _cards(''.join(lines)); first, end = _target(lines, cards, 'surface interaction', name)
        obj = changes['gap_conductance']
        if not isinstance(obj, dict) or obj.get('dependency') not in ('clearance', 'pressure'):
            raise ValueError('导热须明确 dependency 为 clearance 或 pressure，并提供 rows。')
        if set(obj) - {'dependency','rows'}: raise ValueError('导热包含不支持的选项。')
        header = '*Gap Conductance' + (', pressure' if obj['dependency'] == 'pressure' else '') + '\n'
        body = _table(obj, (2, 3), allow_multiple_base_rows=True)
        if any(row[0] < 0 for row in obj['rows']): raise ValueError('接触导热系数必须非负。')
        old = [c for c in cards[first+1:end] if c['keyword'] == 'gap conductance']
        if old and any(k != 'pressure' for k in old[0]['params']): raise ValueError('现有导热属性包含不支持的选项。')
        _edit_card(lines, cards, first, end, 'gap conductance', header + body)
    return _save(source, sp, output_dir, 'interaction_edit', original, ''.join(lines),
                 {'interaction': name, 'changes': list(changes), 'unit_system': spec['unit_system'], 'source': spec['source'],
                  'warning': '导热表会改变默认接触导热行为；分配比例指向次表面，请核对实际接触角色。'})
