"""将用户明确指定的前处理文件归档到独立基准；读取原件，不运行分析。"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from x1.core import digest, new_run
from x1.thermal import audit, read_keywords


def render_settings(records, source, job):
    """逐卡展示参数；大型网格与集合保留原文件行号与行数。"""
    lines=['# 作业前处理设置清单','','作业：`%s`'%job,'输入：`%s`'%source,
           '','按 INP 顺序列出每张关键词卡和数据。网格节点、单元及集合的完整内容保存在同目录 INP 快照和关键词索引中。',
           '本清单记录输入设置；步骤传播、外部子程序编译版本和物理适用性需进一步核对。','']
    large={'node','element','nset','elset'}
    for r in records:
        params=', '.join(k if v is True else k+'='+str(v) for k,v in r['params'].items())
        header='*'+r['keyword']+(', '+params if params else '')
        lines += ['### %s（原输入第 %s 行）'%(header,r['line'])]
        if r['step']:lines.append('分析步：`%s`。'%r['step'])
        if r['keyword'] in large:
            lines.append('数据行数：%s；完整内容见 INP 快照。'%len(r['data']))
        elif r['data']:
            lines.append('```text')
            lines.extend(row['text'] for row in r['data'])
            lines.append('```')
        lines.append('')
    return '\n'.join(lines)


def capture(source, folder, prefix):
    source=Path(source).expanduser().resolve()
    if not source.is_file(): return {'source':str(source),'status':'missing'}
    before=digest(source); data=source.read_bytes(); observed=hashlib.sha256(data).hexdigest(); after=digest(source)
    if before != observed or observed != after:
        return {'source':str(source),'status':'changed_during_read','sha256_before':before,'sha256_after':after}
    target=folder/(prefix+'_'+source.name); target.write_bytes(data)
    return {'source':str(source),'snapshot':str(target),'sha256':observed,
            'size_bytes':len(data),'source_modified_at':datetime.fromtimestamp(source.stat().st_mtime).isoformat(timespec='seconds'),
            'status':'captured'}


def main():
    p=argparse.ArgumentParser(description='将前处理文件和记录保存到独立基准目录')
    p.add_argument('--inp',required=True); p.add_argument('--com'); p.add_argument('--cae'); p.add_argument('--jnl')
    p.add_argument('--subroutine'); p.add_argument('--reference',action='append',default=[])
    p.add_argument('--output-dir',required=True); args=p.parse_args()
    inp=Path(args.inp).expanduser().resolve(); output=Path(args.output_dir).expanduser().resolve()
    sources=[inp]+[Path(v).expanduser().resolve() for v in [args.com,args.cae,args.jnl,args.subroutine] if v]
    sources += [Path(v).expanduser().resolve() for v in args.reference]
    if any(output==f.parent or f.parent in output.parents for f in sources):
        raise ValueError('基准目录不能位于任何原始文件目录或其子目录。')
    if not inp.is_file(): raise ValueError('输入文件不存在。')
    folder=new_run(output,'baseline')
    items={}
    for key in ('inp','com','cae','jnl','subroutine'):
        value=getattr(args,key)
        if value: items[key]=capture(value,folder,key)
    items['references']=[capture(path,folder,'reference_%02d'%i) for i,path in enumerate(args.reference,1)]
    if items['inp']['status'] != 'captured': raise ValueError('INP 读取时变化或不可用，基准未完成。')
    snapshot=Path(items['inp']['snapshot'])
    records,files,warnings=read_keywords(snapshot)
    (folder/'完整关键词索引.json').write_text(json.dumps({'source':str(inp),'snapshot':str(snapshot),
        'source_sha256':items['inp']['sha256'],'records':records,'include_files':files,'warnings':warnings},
        ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'设置清单.md').write_text(render_settings(records,inp,inp.stem),encoding='utf-8')
    report=audit(snapshot)
    (folder/'热材料接触截面检查.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    missing=[name for name, info in items.items() if isinstance(info,dict) and info.get('status') != 'captured']
    missing += [r['source'] for r in items['references'] if r['status'] != 'captured']
    manifest={'recorded_at':datetime.now().isoformat(timespec='seconds'),'job':inp.stem,'items':items,
              'keyword_index':str(folder/'完整关键词索引.json'),'settings_report':str(folder/'设置清单.md'),
              'thermal_audit':str(folder/'热材料接触截面检查.json'),
              'warnings':warnings,'unavailable_or_changed':missing,
              'note':'INP 是该次写出文件的精确快照；CAE/JNL 是采集时独立快照，需核对与作业的一致性。子程序为采集时磁盘版本，编译使用版本需另核对。'}
    (folder/'基准清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 前处理参数基准','','作业：`%s`'%inp.stem,'采集时间：'+manifest['recorded_at'],
           '','## 完整输入及其他文件']
    for key in ('inp','com','cae','jnl','subroutine'):
        item=items.get(key)
        if item: lines.append('- %s：`%s`；状态 %s；SHA-256 `%s`。'%(key,item['source'],item['status'],item.get('sha256','未获得')))
    lines+=['','材料、分析步、接触、载荷、约束、网格、输出、集合和节点等以 INP 精确快照为准；完整关键词及对应行号见 `完整关键词索引.json`。',
           '','逐项可读清单见 `设置清单.md`；热材料、分析步和截面信息见 `热材料接触截面检查.json`。参考资料以清单所列路径保存；资料与当前输入的逐项对应关系需核对。',
            '',manifest['note'],'','后续基准应新增独立版本并对比变化，保留本版本；不以旧资料覆盖已写出的新作业设置。',
            '', '文件缺失或变化：'+(', '.join(missing) if missing else '无'),
            'INCLUDE 读取提示：'+('; '.join(warnings) if warnings else '无')]
    (folder/'基准说明.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'folder':str(folder),'manifest':str(folder/'基准清单.json'),'report':str(folder/'基准说明.md'),
                      'input_sha256':items['inp']['sha256'],'missing':missing,'warnings':warnings},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
