# X1.6 · Abaqus 前处理与网格助手

局部网格选区、分区探测、试划、重新打开复核和续跑命令见 [X1.6 使用说明](X1.6_使用说明.md)。

X1.5 沿用同一个 `x1.py` 入口。`cae-mesh-optimize` 在搜索时同时约束单轮及相对首轮网格的四面体体积。Abaqus 写入副本后出现体积越界、错误或新增警告时，工具回退相关内部节点并重新检查。只有重新打开副本验证通过才发布候选。判定下限为参考单元体积的 25%，是本工具的保护规则。

当前焊头副本已通过实际 CAE 网格验收，见 [X1.5 验收记录](../reports/x15_rollback_replay_20261118_095257_85779f37/验收.md)。

X1.4 在 X1.3 同一套代码上升级，入口仍为 `x1.py`。目录沿用 `x1_1_development`，以兼容已有路径和历史验收资料；不产生另一个并行助手。新增网格警告定位、四面体质量独立核算、固定外边界的实验性内节点优化、CAE 独立副本单因素对照。具体范围和验收状态见 [X1.4 使用说明](X1.4_使用说明.md)。V 系列继续负责结果分析。

原材料 JSON/Excel 映射、接触属性副本编辑、INP 卡片对照和截面控制核查保留，原命令见 [X1.3 使用说明](X1.3_使用说明.md)。

## 给其他 Agent 的接手规则

1. 用户只问怎么操作时，运行 `ask`，阅读最相关结果后结合当前界面说明；记录标注的是资料来源，不能冒充已经验证过的实际界面。
2. 用户明确要求接手时，先确认目标文件、修复对象和具体操作；默认对独立副本进行修改，运行后检查报告。不要使用 Computer Use。
3. 不启动、暂停、中止或重新提交计算；原始 CAE、INP、ODB 和日志保持不变。数据检查也由用户决定并操作。
4. 原工程实时打开时，以用户最后保存的 CAE 为输入。后台副本和当前界面是独立会话；不会自动同步用户尚未保存的修改。

## 两项能力

在此目录打开终端，使用系统 Python：

```powershell
python x1.py ask "网格质量警告怎么检查"
python x1.py ask "刚体焊头约束和参考点"
python x1.py sensor-audit --inp "D:\AbaqusTest\horn_knurl\Job-1.inp"
```

`ask` 返回中文步骤、参考链接和状态。知识库在 `kb/topics.json`，是首批高频条目，并非完整手册；查不到时应查询官方资料并增加经核对的条目，不编造答案。这里的索引采用官方在线文档链接，不包含完整本地手册。

## 热分析前处理

```powershell
python x1.py ask "热力耦合分析步怎么设置"
python x1.py ask "摩擦生热和界面导热"
python x1.py ask "非弹性热份额 塑性生热" --inp "模型输入.inp"
python x1.py thermal-audit --inp "模型输入.inp"
python x1.py thermal-review --inp "模型输入.inp"
python x1.py unit-convert --quantity specific_heat --value 385 --unit "J/(kg*K)"
python x1.py unit-convert --quantity conductivity --value 385 --unit "W/(m*K)"
python x1.py unit-convert --quantity density --value 8900 --unit "kg/m3"
```

`thermal-audit` 只读列出分析步、材料热属性、已识别的部分热位移单元、温度和热接触相关输入以及源文件行号；包含 INCLUDE 时尝试读取并标记缺失。未实现完整 Abaqus 语义、单元分布或各界面属性归属审查，未发现关键词不能直接等同于缺少功能。绝热生热材料缺少显式热属性时提示检查；是否使用由实际研究目标决定。`unit-convert` 仅换算到 N-mm-s-tonne-K，不代替材料值溯源或校验原值的正确性。

`ask --inp` 根据已保存 INP 判断分析类型并筛选教程；存在多种或未识别类型时提示，`--mode coupled|adiabatic|mechanical` 可由用户明确指定。输入文件不代表 CAE 会话里尚未保存的修改。

`thermal-review` 在独立目录写入 `报告.md` 和 `结果.json`，检查已支持的分析步、实例节点及单元、初始温度覆盖、热材料、接触属性与默认值、温度输出、传感器和同名日志。报告包含文件和行号；`ok` 只代表报告生成，不能当作模型通过。`--output-dir` 不得位于原始输入目录下。用户确认模型单位后可指定 `--unit-system N-mm-s-tonne-C` 或 `N-mm-s-tonne-K`；未指定时保持未知。

`--material-record` 可关联材料来源 JSON；仅对明确记录的参数检查与输入是否一致。示例 `config/knurl_thermal_trial_provenance.json` 是本次有齿纹模型试算参数的资料记录，牌号、适用性与单位制仍待用户确认，不能作为其他模型的通用材料库。

```powershell
python x1.py thermal-review --inp "D:\AbaqusTest\Job_Knurl_Thermal_01.inp" --material-record "config\knurl_thermal_trial_provenance.json"
python x1.py journal-add --name knurl_thermal_20261113 --event "核对温度输出" --status "待核对" --note "需以实际 INP 为准"
python x1.py journal-show --name knurl_thermal_20261113
```

操作记录由用户或调用方明确添加，保存在项目 `records` 下；`recorded_at` 是记录时间，不伪造实际操作时间。自动生成的报告和记录不会写入原始模型目录。

用户明确授权且提供目标材料、使用模型单位制的数值后，可在独立 INP 副本中添加比热和非弹性热分数：

```powershell
python x1.py thermal-material-copy --inp "输入.inp" --material Copper --specific-heat 3.85e8 --inelastic-fraction 0.9 --output-dir "非原始作业目录"
```

以上数值只演示命令和单位，不能作为实际材料标定值。复制操作拒绝已含对应属性或 INCLUDE 的输入；输出副本、差异及哈希记录，不修改 CAE，不启用绝热步骤，不运行数据检查。热属性后台修改 CAE 尚未实现；既有 `cae-remesh` 仍未完成真实运行验收。结果、温度云图与可信度分析属于 V 系列。

需要在副本上登记明确的接触力传感器时，指定真实文件、目标区域、分析步和名称：

```powershell
python x1.py sensor-repair --inp "输入文件.inp" --name 传感器名 --surface 实际表面名 --steps Step-1 Step-2 --output-dir "输出目录"
```

此操作只处理每步唯一的单变量 CFNM 历程输出，不适合其他传感器。结果包括新 INP、差异、哈希和中文报告；应由用户进行数据检查。被修改的 INP 不会同步回 CAE。

后台检查或按指定尺寸重划一个零件（使用 Abaqus 官方 CAE 内核，不运行计算）：

```powershell
python x1.py cae-inspect --cae "模型.cae" --model Model-1 --abaqus "abaqus.bat的完整路径" --output-dir "输出目录"
python x1.py cae-remesh --cae "模型.cae" --model Model-1 --part 实际部件名 --size 1.0 --abaqus "abaqus.bat的完整路径" --output-dir "输出目录"
```

重划会删除副本中目标零件的原生网格，保持已有网格控制和单元类型，重新设置全局种子并生成网格；需要检查质量和关联区域，网格变更不保证改善。脚本对 CAE 副本执行，不接管或刷新当前打开的窗口。新建 `runs` 子目录或指定输出目录，每次独立生成。

## 当前验收状态

既有功能见 `X1.2_验收记录.md`，X1.3 的本次验收见 `X1.3_验收记录.md`。解析器只支持一部分 Abaqus 语义；无法识别的内容标注未知，不推断通过。日志仅按同名关联，无法仅凭路径或时间证明对应当前 INP。真实 CAE 副本的 noGUI 检查此前在可访问许可证环境通过；受限环境返回 FlexNet `-63,403`，后台重划仍未完成真实运行验收。

依赖：Windows、Python 3.10+；CAE 后台操作需 Abaqus/CAE 及相应许可证。普通查询和 INP 操作不依赖 Abaqus。

```powershell
$env:TEMP=(Join-Path (Get-Location) 'runs')
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
python -m unittest discover -s tests -v
```
