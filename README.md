# EngineeringAgent

V3.6 正在开发验收，统一入口与新增检查见 [V3.6 开发与使用说明](V3.6_开发与使用说明.md)。

EngineeringAgent 是一个持续升级的 Abaqus 工程分析工具。当前 V3.6 为开发验收版，继承日志诊断、ODB 结果分析、模型输入检查、关键单元异常追踪和多轮工况对照；这些能力属于同一个工具，由统一入口按任务调用。

入口为 `tools/abaqus/diagnose_v3.py`，使用方法、输出与已知限制见 [V3.6 开发与使用说明](V3.6_开发与使用说明.md)。默认只读原始模型及结果，不修改或运行 Abaqus 作业。

只检查日志和同名 INP：

```powershell
python .\tools\abaqus\diagnose_v3.py --directory 'D:\AbaqusTest' --job Job_Flat_Test01
```

只读联合分析 ODB：

```powershell
python .\tools\abaqus\diagnose_v3.py --directory '作业目录' --job '作业名' --odb '作业ODB完整路径'
```

需要提取指定参考点历程、预压步或按用户阈值分析时，使用同一入口的 `results` 模式：

```powershell
python .\tools\abaqus\diagnose_v3.py results --odb '作业ODB完整路径' --rp-history-region '已确认的历程输出区域'
```

参考点、单位、方向和预压步均需对应实际作业核实，不可按示例推断。
