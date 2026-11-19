"""对指定真实 INP 做复制与对照验证；生成物只存独立目录，不提交作业。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x1.core import digest, new_run
from x1.edit import _cards, _target, interaction_edit, material_write
from x1.thermal import audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inp',required=True); parser.add_argument('--material',required=True)
    parser.add_argument('--interaction',required=True); parser.add_argument('--conversion',required=True,type=float)
    parser.add_argument('--output',required=True); args = parser.parse_args()
    inp=Path(args.inp).resolve(); root=Path(args.output).resolve()
    if root == inp.parent or inp.parent in root.parents: raise ValueError('验证目录不能位于原始工程目录。')
    out=new_run(root,'real_input')
    before=digest(inp); text=inp.read_bytes().decode('latin-1'); lines,cards=_cards(text)
    start,end=_target(lines,cards,'material',args.material)
    names={'density':'density','elastic':'elastic','conductivity':'conductivity',
           'specific heat':'specific_heat','expansion':'expansion','plastic':'plastic_johnson_cook',
           'rate dependent':'rate_johnson_cook','inelastic heat fraction':'inelastic_heat_fraction'}
    props={}
    for card in cards[start+1:end]:
        key=names.get(card['keyword'])
        if not key: continue
        if key in props: raise ValueError('材料卡存在重复关键词，停止。')
        rows=[]
        for row in lines[card['start']+1:card['end']]:
            if row.strip() and not row.lstrip().startswith('**'):
                rows.append([float(v.strip()) for v in row.split(',') if v.strip()])
        props[key]={'rows':rows}
        if key=='expansion' and 'zero' in card['params']:
            props[key]['zero']=float(card['params']['zero'])
    mat={'material':args.material,'unit_system':'沿用源文件，验证时不转换','source':str(inp),'properties':props}
    contact={'interaction':args.interaction,'unit_system':'沿用源文件，验证时不转换','source':'指定试验输入',
             'changes':{'heat_conversion':args.conversion}}
    matpath=out/'material_test.json'; matpath.write_text(json.dumps(mat,ensure_ascii=False,indent=2),encoding='utf-8')
    contactpath=out/'contact_test.json'; contactpath.write_text(json.dumps(contact,ensure_ascii=False,indent=2),encoding='utf-8')
    a=material_write(inp,matpath,out); b=interaction_edit(inp,contactpath,out)
    sections=audit(inp)['sections']; after=digest(inp)
    summary={'input':str(inp),'sha256_before':before,'sha256_after':after,'input_unchanged':before==after,
             'material_properties_tested':list(props),'material_copy':a['output'],
             'material_outside_identical':a['outside_target_identical'],
             'interaction_copy':b['output'],'interaction_outside_identical':b['outside_target_identical'],
             'interaction_card_changes':b['semantic_diff']['card_changes'],'sections':sections}
    (out/'真实输入只读验证.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
