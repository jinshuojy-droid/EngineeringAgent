"""Abaqus INP 的只读索引；只报告文件直接支持的关联，不猜测零件用途。"""
from __future__ import annotations

import hashlib
from pathlib import Path


# 没有列在这里的关键字会记录为未解析，而不是默默丢失。
_CATEGORIES = {
    'part': 'parts', 'instance': 'instances', 'assembly': 'assemblies',
    'node': 'nodes', 'element': 'elements', 'nset': 'node_sets',
    'elset': 'element_sets', 'surface': 'surfaces', 'material': 'materials',
    'solid section': 'sections', 'shell section': 'sections',
    'cohesive section': 'sections', 'surface interaction': 'contact_properties',
    'friction': 'contact_properties', 'surface behavior': 'contact_properties',
    'contact pair': 'contacts', 'contact': 'contacts',
    'contact inclusions': 'contacts', 'contact exclusions': 'contacts',
    'contact property assignment': 'contacts', 'tie': 'constraints',
    'coupling': 'constraints', 'kinematic': 'constraints',
    'distributing': 'constraints', 'rigid body': 'constraints',
    'equation': 'constraints', 'boundary': 'boundary_conditions',
    'cload': 'loads', 'dload': 'loads', 'dsload': 'loads',
    'step': 'steps', 'dynamic': 'steps', 'static': 'steps',
    'elastic': 'material_properties', 'plastic': 'material_properties',
    'density': 'material_properties', 'rate dependent': 'material_properties',
    'amplitude': 'amplitudes', 'section controls': 'section_controls',
    'output': 'outputs', 'node output': 'outputs', 'contact output': 'outputs',
}
_ENDS = {'end part': 'part', 'end instance': 'instance',
         'end assembly': 'assembly', 'end step': 'step'}
_CATEGORIES_LIST = tuple(sorted(set(_CATEGORIES.values())))


def _evidence(path, line, text):
    return {'file': str(path), 'line': line, 'text': text.rstrip()}


def _keyword(text):
    """解析关键字和参数；支持带引号的逗号，不改变参数值大小写。"""
    fields, token, quote = [], [], None
    for char in text[1:]:
        if char in ('"', "'"):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            token.append(char)
        elif char == ',' and quote is None:
            fields.append(''.join(token))
            token = []
        else:
            token.append(char)
    if quote is not None:
        raise ValueError('未闭合的引号')
    fields.append(''.join(token))
    name = fields[0].strip().lower()
    params = {}
    for field in fields[1:]:
        field = field.strip()
        if not field:
            continue
        key, sep, value = field.partition('=')
        params[key.strip().lower()] = value.strip().strip('"\'') if sep else True
    return name, params


def parse_inp(path):
    """只读解析 INP。include 按其所在文件目录解析；文件或语法不完整会报告。

    返回 JSON 对象：records 中每条记录含 scope、parameters、evidence 和 data；
    data 的每条记录亦含来源行。complete 仅指文件可读且 include 图完整，
    unsupported_keywords 非空时不能理解为完整解析了模型语义。
    """
    root = Path(path).expanduser().resolve()
    result = {'schema_version': '3.0', 'input_file': str(root), 'files': [],
              'records': {key: [] for key in _CATEGORIES_LIST}, 'warnings': [],
              'unsupported_keywords': [], 'complete': True}
    scope = {'part': None, 'instance': None, 'assembly': None,
             'material': None, 'step': None}
    current = None
    unknown = {}

    def warn(message, evidence=None):
        result['warnings'].append({'message': message, 'evidence': evidence})
        result['complete'] = False

    def visit(file_path, stack, include_evidence=None):
        nonlocal current
        file_path = file_path.resolve()
        if file_path in stack:
            warn('检测到循环 INCLUDE，已停止该分支。', include_evidence)
            return
        if len(stack) >= 32:
            warn('INCLUDE 嵌套超过 32 层，已停止该分支。', include_evidence)
            return
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            warn('无法读取 INP 或 INCLUDE：%s (%s)' % (file_path, exc), include_evidence)
            return
        text = None
        for encoding in ('utf-8-sig', 'gb18030', 'cp1252'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            warn('无法解码 INP 文件：%s' % file_path, include_evidence)
            return
        result['files'].append({'path': str(file_path), 'sha256': hashlib.sha256(raw).hexdigest(),
                                'encoding': encoding})
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line_no = i + 1
            original = lines[i]
            i += 1
            line = original.strip()
            if not line or line.startswith('**'):
                continue
            if line.startswith('*'):
                combined = line
                while combined.endswith(',') and i < len(lines):
                    continuation = lines[i].strip()
                    if not continuation or continuation.startswith('**'):
                        i += 1
                        continue
                    if continuation.startswith('*'):
                        break
                    combined += continuation
                    i += 1
                try:
                    name, params = _keyword(combined)
                except (ValueError, IndexError) as exc:
                    warn('关键字无法解析：%s' % exc, _evidence(file_path, line_no, original))
                    current = None
                    continue
                evidence = _evidence(file_path, line_no, original)
                if name == 'include':
                    include = params.get('input')
                    if not isinstance(include, str) or not include:
                        warn('INCLUDE 缺少 INPUT 参数。', evidence)
                        continue
                    candidate = Path(include.replace('\\', '/'))
                    visit(candidate if candidate.is_absolute() else file_path.parent / candidate,
                          stack + (file_path,), evidence)
                    continue
                if name in _ENDS:
                    scope[_ENDS[name]] = None
                    if name == 'end part':
                        scope['material'] = None
                    current = None
                    continue
                if name in ('part', 'instance', 'assembly', 'material', 'step'):
                    scope[name] = params.get('name')
                category = _CATEGORIES.get(name)
                if category:
                    current = {'keyword': name, 'parameters': params,
                               'scope': dict(scope), 'evidence': evidence, 'data': []}
                    result['records'][category].append(current)
                else:
                    current = None
                    if name not in unknown:
                        unknown[name] = {'keyword': name, 'count': 0, 'evidence': evidence}
                    unknown[name]['count'] += 1
            elif current is not None:
                current['data'].append({'text': line, 'evidence': _evidence(file_path, line_no, original)})
        # 当前数据块可跨 include 边界；保持关键字和作用域。

    visit(root, ())
    result['unsupported_keywords'] = list(unknown.values())
    return result


def _same(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.casefold() == b.casefold()


def _contains_element(block, label):
    """返回 True、False、None（含无法判定的嵌套集合或不支持的表达式）。"""
    uncertain = False
    for entry in block['data']:
        values = [s.strip() for s in entry['text'].split(',') if s.strip()]
        try:
            nums = [int(v) for v in values]
        except ValueError:
            uncertain = True
            continue
        if 'generate' in block['parameters']:
            if len(nums) not in (2, 3):
                uncertain = True
                continue
            first, last = nums[:2]
            stride = nums[2] if len(nums) == 3 else 1
            if stride <= 0 or last < first:
                uncertain = True
            elif first <= label <= last and (label - first) % stride == 0:
                return True
        elif label in nums:
            return True
    return None if uncertain else False


def locate_element(result, instance, label):
    """定位显式实例/零件/单元及可直接证明的截面；未知与歧义均不猜测。"""
    label = int(label)
    out = {'instance': instance, 'element': label, 'status': 'unknown',
           'part': None, 'element_type': None, 'sections': [], 'evidence': [],
           'limitations': []}
    records = result['records']
    matches = [x for x in records['instances'] if _same(x['parameters'].get('name'), instance)]
    if len(matches) != 1 or not matches[0]['parameters'].get('part'):
        out['limitations'].append('实例不存在、重复或未直接引用 PART。')
        return out
    inst = matches[0]
    part = inst['parameters']['part']
    out['part'] = part
    out['evidence'].append(inst['evidence'])
    elements = []
    for block in records['elements']:
        if not (_same(block['scope']['part'], part) or _same(block['scope']['instance'], instance)):
            continue
        for row in block['data']:
            try:
                row_label = int(row['text'].split(',', 1)[0].strip())
            except ValueError:
                continue
            if row_label == label:
                elements.append((block, row))
    if len(elements) != 1:
        out['limitations'].append('单元未找到或定义不唯一；不支持通过生成关键字推断单元。')
        return out
    block, row = elements[0]
    out['status'] = 'found'
    out['element_type'] = block['parameters'].get('type')
    out['evidence'].extend([block['evidence'], row['evidence']])
    for section in records['sections']:
        if not _same(section['scope']['part'], part):
            continue
        set_name = section['parameters'].get('elset')
        if not set_name:
            continue
        state = []
        if _same(block['parameters'].get('elset'), set_name):
            state.append(True)
        for item in records['element_sets']:
            if _same(item['scope']['part'], part) and _same(item['parameters'].get('elset'), set_name):
                state.append(_contains_element(item, label))
        if True in state:
            evidence = [section['evidence']]
            for item in records['element_sets']:
                if not (_same(item['scope']['part'], part)
                        and _same(item['parameters'].get('elset'), set_name)):
                    continue
                for data_row in item['data']:
                    one_row = dict(item, data=[data_row])
                    if _contains_element(one_row, label) is True:
                        evidence.extend([item['evidence'], data_row['evidence']])
            out['sections'].append({'name': set_name, 'material': section['parameters'].get('material'),
                                    'evidence': evidence})
        elif None in state:
            out['limitations'].append('截面集合 %s 含嵌套集合或不支持的成员表达式。' % set_name)
    if not out['sections']:
        out['limitations'].append('未能直接证明单元所属截面和材料，可能使用了装配级集合或其他定义方式。')
    if not result['complete']:
        out['limitations'].append('输入文件或 INCLUDE 不完整，结果可能缺失。')
    return out
