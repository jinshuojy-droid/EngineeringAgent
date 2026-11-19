"""Abaqus/CAE noGUI：将实际存在的末帧场变量导出为 PNG。"""
import argparse
import json
import os
from abaqus import session
from abaqusConstants import CONTOURS_ON_DEF, INTEGRATION_POINT, INVARIANT, MAGNITUDE, MISES, NODAL, OFF, PNG, UNIFORM
import visualization


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--odb',required=True);p.add_argument('--output',required=True)
    p.add_argument('--camera-config', help='可选视角 JSON 文件')
    p.add_argument('--frames',type=int,default=1,help='末分析步均匀选取的已保存帧数')
    a,_=p.parse_known_args()
    camera = None
    if a.camera_config:
        with open(a.camera_config, encoding='utf-8') as f:
            camera = json.load(f)
    odb=session.openOdb(name=a.odb,readOnly=True)
    results=[]
    try:
        steps=list(odb.steps.keys())
        if not steps: raise ValueError('ODB 没有分析步')
        step_name=steps[-1];step=odb.steps[step_name]
        if not len(step.frames): raise ValueError('末分析步没有输出帧')
        if a.frames < 1: raise ValueError('帧数必须为正')
        vp=session.Viewport(name='EngineeringAgentReview', origin=(0,0), width=220, height=160)
        vp.setValues(displayedObject=odb)
        if camera:
            vp.viewportAnnotationOptions.setValues(title=OFF, statePosition=(60, 95),
                stateFont='-*-verdana-medium-r-*-*-*-80-*-*-*-*-iso8859-1',
                legendFont='-*-verdana-medium-r-*-*-*-90-*-*-*-*-iso8859-1')
        vp.odbDisplay.display.setValues(plotState=(CONTOURS_ON_DEF,))
        vp.odbDisplay.commonOptions.setValues(deformationScaling=UNIFORM,uniformScaleFactor=1.0)
        indices = [len(step.frames)-1] if a.frames == 1 else sorted(set(round(i*(len(step.frames)-1)/(a.frames-1)) for i in range(a.frames)))
        for frame_index in indices:
          frame=step.frames[frame_index]
          vp.odbDisplay.setFrame(step=len(steps)-1,frame=frame_index)
          for label in ('U','S','PEEQ','CPRESS'):
            names=[n for n in frame.fieldOutputs.keys() if n==label or (label=='CPRESS' and n.startswith('CPRESS'))]
            if not names:
                results.append({'variable':label,'step':step_name,'frame':frame_index,'time':float(frame.frameValue),'status':'missing'});continue
            for index,name in enumerate(names):
                try:
                    kwargs={'variableLabel':name,'outputPosition':NODAL if label in ('U','CPRESS') else INTEGRATION_POINT}
                    if label=='U':kwargs['refinement']=(INVARIANT,'Magnitude')
                    if label=='S':kwargs['refinement']=(INVARIANT,'Mises')
                    try:
                        vp.odbDisplay.setPrimaryVariable(**kwargs)
                        method='variableLabel'
                    except Exception:
                        if label != 'CPRESS':raise
                        # 接触对名称并非总能作为 GUI 主变量标签，尝试直接指定该帧字段。
                        vp.odbDisplay.setPrimaryVariable(field=frame.fieldOutputs[name],outputPosition=NODAL)
                        method='field'
                    vp.view.fitView()
                    if camera:
                        vp.view.setValues(cameraPosition=tuple(camera['position']),
                                          cameraTarget=tuple(camera['target']),
                                          cameraUpVector=tuple(camera['up']),
                                          width=float(camera['width']), height=float(camera['height']))
                    suffix = '' if a.frames == 1 else '_frame'+str(frame_index)
                    output=os.path.join(a.output, label+'_'+str(index)+suffix)
                    session.printToFile(fileName=output,format=PNG,canvasObjects=(vp,))
                    results.append({'variable':name,'step':step_name,'frame':frame_index,'time':float(frame.frameValue),
                                    'scale_factor':1.0,'camera':camera,'file':output+'.png','status':'ok','selection_method':method})
                except Exception as e:
                    results.append({'variable':name,'step':step_name,'frame':frame_index,'time':float(frame.frameValue),'status':'error','error':str(e)})
    finally:
        odb.close()
        with open(os.path.join(a.output,'contours.json'),'w',encoding='utf-8') as f:
            json.dump(results,f,ensure_ascii=False,indent=2)


if __name__=='__main__':main()
