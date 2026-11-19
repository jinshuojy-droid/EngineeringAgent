"""记录用户报告或文件核对过的操作，不伪造执行时间和自动操作。"""
from datetime import datetime
import json
from pathlib import Path
import re

from .core import ROOT


def journal_path(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
        raise ValueError('记录名称仅允许字母、数字、下划线和短横线')
    return ROOT / 'records' / (name + '.jsonl')


def add(name, event, status, evidence=None, note=None):
    if not event.strip():raise ValueError('记录内容不能为空')
    if status not in ('用户说明','输入文件已核对','待核对'):
        raise ValueError('记录状态不受支持')
    path=journal_path(name)
    path.parent.mkdir(parents=True,exist_ok=True)
    entry={'recorded_at':datetime.now().isoformat(timespec='seconds'),'event':event.strip(),
           'status':status,'evidence':evidence,'note':note,
           'time_note':'记录时间不是操作执行时间'}
    with path.open('a',encoding='utf-8',newline='\n') as stream:
        stream.write(json.dumps(entry,ensure_ascii=False)+'\n')
    return {'path':str(path),'entry':entry}


def show(name):
    path=journal_path(name)
    if not path.is_file():return {'path':str(path),'events':[]}
    events=[]
    with path.open(encoding='utf-8') as stream:
        for line_no,line in enumerate(stream,1):
            if not line.strip():continue
            try:events.append(json.loads(line))
            except json.JSONDecodeError as exc:raise ValueError('操作记录第 %d 行无效'%line_no) from exc
    return {'path':str(path),'events':events}
