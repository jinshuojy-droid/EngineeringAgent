"""统一入口的 Abaqus Python 局部只读提取任务。"""
import argparse
import os
import shutil
import subprocess
from pathlib import Path
from result_mode import PROJECT, new_output, within


def extract_target(odb, instance, element, root, abaqus=None):
    odb=Path(odb).resolve();root=Path(root).resolve()
    if not odb.is_file() or odb.suffix.lower()!='.odb' or element<=0:raise ValueError('无效的 ODB 文件或单元编号')
    if not within(root,PROJECT) or within(root,odb.parent) or within(root,(PROJECT/'versions').resolve()):raise ValueError('输出路径必须在项目内且不能位于源目录或版本目录')
    command=abaqus or shutil.which('abaqus') or shutil.which('abaqus.bat')
    if not command:raise ValueError('找不到 Abaqus Python 命令')
    out=new_output(root);target=out/'extracted';env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
    cmd=[str(command),'python',str(PROJECT/'tools'/'abaqus'/'target_extract.py'),'--odb',str(odb),'--instance',instance,'--element',str(element),'--output',str(target)]
    proc=subprocess.run(cmd,cwd=str(out),env=env,capture_output=True,text=True,encoding='utf-8',errors='replace')
    (out/'extractor.log').write_text('exit_code=%s\nstdout:\n%s\nstderr:\n%s'%(proc.returncode,proc.stdout,proc.stderr),encoding='utf-8')
    if proc.returncode or not (target/'target_manifest.json').is_file():raise ValueError('局部提取失败：'+str(out/'extractor.log'))
    return target


def main(argv=None):
    p=argparse.ArgumentParser(description='只读提取指定单元和邻域，不运行求解器')
    p.add_argument('--odb',required=True);p.add_argument('--instance',required=True);p.add_argument('--element',type=int,required=True)
    p.add_argument('--abaqus');p.add_argument('--output',default=str(PROJECT/'reports'/'target'))
    a=p.parse_args(argv)
    target=extract_target(a.odb,a.instance,a.element,a.output,a.abaqus)
    print('EngineeringAgent 局部结果目录：'+str(target));return 0
