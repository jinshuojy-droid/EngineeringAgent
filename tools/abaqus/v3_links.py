"""从 INP 直接定义追溯单元、表面与显式接触/Tie 引用，不推断因果。"""


def _same(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.casefold() == right.casefold()


def _elset_row_match(row, block, instance, label):
    """返回 (命中, 未支持格式)。仅匹配明确属于目标实例的成员。"""
    params = block['parameters']
    block_instance = params.get('instance') or block['scope'].get('instance')
    parts = [x.strip() for x in row['text'].split(',') if x.strip()]
    if 'generate' in params:
        if not block_instance:
            return False, True
        if not _same(block_instance, instance):
            return False, False
        try:
            numbers = [int(x) for x in parts]
            if len(numbers) != 3 or numbers[2] <= 0 or numbers[1] < numbers[0]:
                return False, True
            start, end, stride = numbers
            return start <= label <= end and (label - start) % stride == 0, False
        except ValueError:
            return False, True
    unknown = False
    for item in parts:
        if item.isdigit():
            if not block_instance:
                unknown = True
            elif _same(block_instance, instance) and int(item) == label:
                return True, unknown
        else:
            prefix, dot, suffix = item.rpartition('.')
            if dot and suffix.isdigit():
                if block_instance and not _same(block_instance, prefix):
                    unknown = True
                elif _same(prefix, instance) and int(suffix) == label:
                    return True, unknown
            else:
                unknown = True
    return False, unknown


def link_element(parsed, instance, label):
    """从已解析 INP 获取该实例单元直接表面与关系引用，所有命中附输入行证据。"""
    if not isinstance(instance, str) or not instance or not isinstance(label, int) or label <= 0:
        raise ValueError('必须指定实例名称和正整数单元标签')
    records = parsed['records']
    sets = records.get('element_sets', [])
    surfaces = []
    unknown_count = 0
    for surface in records.get('surfaces', []):
        if not surface['scope'].get('assembly') or surface['scope'].get('part'):
            unknown_count += 1
            continue
        if not _same(surface['parameters'].get('type', 'ELEMENT'), 'ELEMENT'):
            unknown_count += 1
            continue
        name = surface['parameters'].get('name')
        if not name:
            unknown_count += 1
            continue
        for row in surface['data']:
            parts = [x.strip() for x in row['text'].split(',')]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                unknown_count += 1
                continue
            set_name, face = parts[:2]
            hit = []
            prefix, dot, suffix = set_name.rpartition('.')
            if dot and suffix.isdigit() and _same(prefix, instance) and int(suffix) == label:
                hit.append({'kind': '直接指定实例单元', 'evidence': row['evidence']})
            candidates = [s for s in sets if s['scope'].get('assembly')
                          and not s['scope'].get('part')
                          and _same(s['parameters'].get('elset'), set_name)]
            if not candidates and not (dot and suffix.isdigit()):
                unknown_count += 1
            for block in candidates:
                for member in block['data']:
                    matched, unsupported = _elset_row_match(member, block, instance, label)
                    if unsupported:
                        unknown_count += 1
                    if matched:
                        hit.append({'kind': '装配单元集合直接成员',
                                    'set': set_name, 'set_evidence': block['evidence'],
                                    'member_evidence': member['evidence']})
            if hit:
                surfaces.append({'name': name, 'face': face, 'set': set_name,
                                 'surface_evidence': surface['evidence'],
                                 'definition_evidence': row['evidence'], 'membership': hit})
    relationships = []
    for block in records.get('contacts', []) + records.get('constraints', []):
        if block['keyword'] not in ('contact pair', 'contact inclusions', 'tie'):
            continue
        if block['keyword'] == 'contact inclusions' and 'all exterior' in block['parameters']:
            unknown_count += 1
        for row in block['data']:
            sides = [x.strip() for x in row['text'].split(',')]
            if len(sides) < 2 or not sides[0] or not sides[1]:
                unknown_count += 1
                continue
            for surface in surfaces:
                for side in (0, 1):
                    if _same(sides[side], surface['name']):
                        relationships.append({'kind': block['keyword'], 'surface': surface['name'],
                                              'face': surface['face'], 'surface_side': side + 1,
                                              'other_surface': sides[1 - side],
                                              'step': block['scope'].get('step'),
                                              'parameters': block['parameters'],
                                              'keyword_evidence': block['evidence'],
                                              'reference_evidence': row['evidence'],
                                              'surface_definition_evidence': surface['definition_evidence']})
    limitations = ['仅支持装配级 ELEMENT 表面和直接定义的集合成员；缺少命中不等于没有接触或约束。',
                   '引用仅证明输入定义，不证明求解中生效、发生接触、相互作用或造成畸变。',
                   '不解析嵌套集合、通用接触的自动全表面、面别组合、节点面、解析刚体表面及接触属性继承。']
    if unknown_count:
        limitations.append('有 %s 个面定义或集合成员不在支持范围，未作推断。' % unknown_count)
    if not parsed.get('complete', False):
        limitations.append('输入文件或 INCLUDE 不完整，结果可能缺失。')
    return {'instance': instance, 'element': label, 'surfaces': surfaces,
            'relationships': relationships, 'unknown_count': unknown_count,
            'limitations': limitations}
