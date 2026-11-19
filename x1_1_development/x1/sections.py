"""识别零件截面、单元集合与显式沙漏设置，未知映射保持未知。"""
from collections import Counter


def section_audit(records):
    """按零件范围解析常见实体单元和 ELSET；保留源文件行号。"""
    counts = {'C3D8R':8, 'C3D8RT':8, 'C3D8':8, 'C3D8T':8, 'C3D4':4,
              'C3D4T':4, 'C3D10':10, 'C3D10M':10, 'C3D10MT':10, 'C3D6':6, 'C3D6T':6}
    parts = {}; controls = {}; sections = []; part = None
    previous = None
    for rec in records:
        key, p = rec['keyword'], rec['params']
        if key == 'part':
            part = str(p.get('name', '?'))
            parts.setdefault(part.casefold(), {'name': part, 'elements': {}, 'sets': {}, 'unknown': []})
        elif key == 'end part': part = None
        elif key == 'section controls':
            controls.setdefault(str(p.get('name', '')).casefold(), []).append(rec)
        elif key == 'solid section':
            sections.append({'part': part, 'section': rec})
        elif key == 'hourglass stiffness' and previous == 'solid section' and sections:
            sections[-1]['hourglass_stiffness'] = rec
        elif part and key == 'element':
            group = parts[part.casefold()]; typ = str(p.get('type', '')).upper()
            if typ not in counts:
                group['unknown'].append({'file':rec['file'], 'line':rec['line'], 'type':typ}); continue
            remaining = 0; labels = []
            try:
                for row in rec['data']:
                    vals = [x.strip() for x in row['text'].split(',') if x.strip()]
                    while vals:
                        if remaining == 0:
                            label = int(vals.pop(0)); labels.append(label); remaining = counts[typ]
                            if label in group['elements']: raise ValueError('重复单元编号')
                            group['elements'][label] = typ
                        take = min(len(vals), remaining)
                        for value in vals[:take]: int(value)
                        del vals[:take]; remaining -= take
                if remaining: raise ValueError('单元连接不完整')
                if p.get('elset'):
                    group['sets'].setdefault(str(p['elset']).casefold(), []).append(('ids', set(labels)))
            except ValueError:
                group['unknown'].append({'file':rec['file'], 'line':rec['line'], 'type':typ, 'reason':'单元连接无法解析'})
        elif part and key == 'elset':
            group = parts[part.casefold()]; name = str(p.get('elset','')).casefold()
            if p.get('instance'):
                group['unknown'].append({'file':rec['file'], 'line':rec['line'], 'reason':'跨实例 ELSET 未解析'}); continue
            tokens = [x.strip() for row in rec['data'] for x in row['text'].split(',') if x.strip()]
            entries = group['sets'].setdefault(name, [])
            try:
                if 'generate' in p:
                    nums = [int(x) for x in tokens]
                    if len(nums) % 3: raise ValueError('generate 格式错误')
                    for i in range(0,len(nums),3):
                        lo, hi, step = nums[i:i+3]
                        if step <= 0 or hi < lo or (hi-lo)//step > 5000000: raise ValueError('generate 范围无效')
                        entries.append(('ids', set(range(lo,hi+1,step))))
                else:
                    for value in tokens:
                        entries.append(('ids', {int(value)}) if value.isdigit() else ('ref', value.casefold()))
            except ValueError:
                group['unknown'].append({'file':rec['file'], 'line':rec['line'], 'reason':'ELSET 无法解析'})
        previous = key

    def resolve(group, name, seen):
        if name in seen or name not in group['sets']: return set(), False
        values = set(); valid = True
        for kind, item in group['sets'][name]:
            if kind == 'ids': values.update(item)
            else:
                subset, ok = resolve(group, item, seen | {name}); values.update(subset); valid = valid and ok
        return values, valid

    result = []
    for item in sections:
        rec = item['section']; p = rec['params']; part = item['part']
        entry = {'part': part, 'elset': p.get('elset'), 'material': p.get('material'),
                 'file': rec['file'], 'line': rec['line'], 'controls': p.get('controls'),
                 'hourglass': '未显式指定；按分析类型和单元适用默认规则核查', 'element_types': [], 'complete': False}
        if item.get('hourglass_stiffness'):
            rec_h = item['hourglass_stiffness']
            entry['hourglass_stiffness'] = {'file':rec_h['file'], 'line':rec_h['line'], 'data':rec_h['data']}
        if p.get('controls'):
            hits = controls.get(str(p['controls']).casefold(), [])
            if len(hits) == 1:
                entry['hourglass'] = hits[0]['params'].get('hourglass', '未在 SECTION CONTROLS 显式指定')
                entry['controls_evidence'] = {'file': hits[0]['file'], 'line': hits[0]['line']}
            else: entry['hourglass'] = '截面控制未找到或名称不唯一'
        group = parts.get(str(part).casefold()) if part else None
        if group and p.get('elset'):
            labels, ok = resolve(group, str(p['elset']).casefold(), set())
            types = Counter(group['elements'][label] for label in labels if label in group['elements'])
            entry['element_types'] = [{'type': k, 'count': v} for k,v in sorted(types.items())]
            entry['complete'] = ok and bool(labels) and len(labels.intersection(group['elements'])) == len(labels) and not group['unknown']
            if not entry['complete']: entry['mapping_note'] = '集合或单元类型未完整解析；需人工核对。'
        else: entry['mapping_note'] = '非零件级截面或缺少集合，需人工核对。'
        result.append(entry)
    return result
