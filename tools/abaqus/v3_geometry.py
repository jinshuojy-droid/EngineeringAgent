"""只读评估线性六面体的几何变化，不依赖 Abaqus 运行环境。

坐标必须按 Abaqus C3D8 连接顺序提供，变形坐标需由调用方计算为初始坐标加位移。
采样雅可比只描述九个指定位置，不能证明整个单元内部均有效。
"""

import math

_SIGNS = ((-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
          (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))
_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6),
          (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))
_EPS = 1e-12


def _coordinates(values):
    if values is None:
        raise ValueError('missing coordinates')
    if len(values) != 8:
        raise ValueError('expected eight nodes')
    result = []
    for point in values:
        if point is None or len(point) != 3 or any(x is None for x in point):
            raise ValueError('missing coordinate component')
        vector = tuple(float(x) for x in point)
        if not all(math.isfinite(x) for x in vector):
            raise ValueError('non-finite coordinate')
        result.append(vector)
    return result


def _jacobian(coords, natural):
    xi, eta, zeta = natural
    matrix = [[0.0] * 3 for _ in range(3)]
    for point, (sx, sy, sz) in zip(coords, _SIGNS):
        derivatives = (sx * (1 + sy * eta) * (1 + sz * zeta) / 8.0,
                       sy * (1 + sx * xi) * (1 + sz * zeta) / 8.0,
                       sz * (1 + sx * xi) * (1 + sy * eta) / 8.0)
        for row in range(3):
            for col in range(3):
                matrix[row][col] += point[row] * derivatives[col]
    a, b, c = matrix
    determinant = (a[0] * (b[1]*c[2] - b[2]*c[1])
                   - a[1] * (b[0]*c[2] - b[2]*c[0])
                   + a[2] * (b[0]*c[1] - b[1]*c[0]))
    norms = [math.sqrt(sum(matrix[row][col] ** 2 for row in range(3)))
             for col in range(3)]
    denominator = norms[0] * norms[1] * norms[2]
    scaled = determinant / denominator if denominator else None
    if not math.isfinite(determinant) or (scaled is not None and not math.isfinite(scaled)):
        raise ValueError('non-finite Jacobian')
    if determinant < 0:
        condition = 'inverted'
    elif scaled is None or scaled <= _EPS:
        condition = 'degenerate'
    else:
        condition = 'positive'
    return {'natural_coordinates': list(natural), 'determinant': determinant,
            'scaled_jacobian': scaled, 'condition': condition}


def _metrics(coords, labels):
    samples = []
    for idx, natural in enumerate(_SIGNS + ((0, 0, 0),)):
        sample = _jacobian(coords, natural)
        sample['location'] = 'corner' if idx < 8 else 'center'
        sample['node_label'] = labels[idx] if idx < 8 else None
        samples.append(sample)
    edges = []
    for first, second in _EDGES:
        length = math.dist(coords[first], coords[second])
        if not math.isfinite(length):
            raise ValueError('non-finite edge length')
        edges.append({'node_a': labels[first], 'node_b': labels[second], 'length': length})
    return {'jacobian_samples': samples,
            'minimum_jacobian': min(s['determinant'] for s in samples),
            'minimum_scaled_jacobian': min((s['scaled_jacobian'] for s in samples
                                            if s['scaled_jacobian'] is not None), default=None),
            'edge_lengths': edges, 'shortest_edge': min(e['length'] for e in edges)}


def assess_element(element_type, node_labels, reference_coordinates, deformed_coordinates,
                   provenance=None):
    """返回可 JSON 序列化的结果；provenance 由调用者提供并原样保留。"""
    result = {'status': 'unknown', 'element_type': element_type, 'node_labels': node_labels,
              'provenance': provenance if provenance is not None else {'status': 'unknown'},
              'reference': None, 'deformed': None, 'edge_ratios': [],
              'limitations': ['仅支持 C3D8/C3D8R；九个采样点不保证整个单元有效。',
                              '结果不是 Abaqus 内置网格质量指标；不判断异常成因。']}
    if not isinstance(element_type, str) or element_type.upper().strip() not in ('C3D8', 'C3D8R'):
        result['status'] = 'unsupported_element'
        return result
    if node_labels is None or reference_coordinates is None or deformed_coordinates is None:
        result['status'] = 'missing_data'
        return result
    try:
        if len(node_labels) != 8 or len(set(node_labels)) != 8 or any(x is None for x in node_labels):
            raise ValueError('expected eight distinct node labels')
        result['node_labels'] = list(node_labels)
        initial = _coordinates(reference_coordinates)
        current = _coordinates(deformed_coordinates)
        result['reference'] = _metrics(initial, result['node_labels'])
        result['deformed'] = _metrics(current, result['node_labels'])
        for old, new in zip(result['reference']['edge_lengths'], result['deformed']['edge_lengths']):
            result['edge_ratios'].append({'node_a': old['node_a'], 'node_b': old['node_b'],
                                          'reference_length': old['length'],
                                          'deformed_length': new['length'],
                                          'ratio': new['length'] / old['length'] if old['length'] else None})
        if any(s['condition'] != 'positive' for s in result['reference']['jacobian_samples']):
            result['status'] = 'invalid_reference'
        elif any(s['condition'] != 'positive' for s in result['deformed']['jacobian_samples']):
            result['status'] = 'nonpositive_jacobian'
        else:
            result['status'] = 'ok'
    except (ValueError, TypeError, OverflowError) as exc:
        result['status'] = 'invalid_input'
        result['error'] = str(exc)
        result['reference'] = None
        result['deformed'] = None
        result['edge_ratios'] = []
    return result
