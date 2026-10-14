# 可视化编排：形状调研与建议

> 调研问题：**给定一串"发给所有狗、发完就等一个声明时长"的线性步骤，正确的编辑形状是什么，UI 应该长什么样？**
>
> 结论先行：**用户说"想要可视化编程"，但节点图是错的形状。这个问题的正确形状是提示单（cue list）——QLab 的 auto-follow/auto-continue、IEC 61131-3 的顺序功能图（SFC）、以及 Boston Dynamics Choreographer 的时间线，三者独立地收敛到同一个形状。当前模型已经是这个形状了；坏掉的是渲染、是三列缺失的信息、是两个缺失的结构装置。**
>
> 本文中所有关于本仓库的断言都来自读代码，并给出文件与行号。所有外部断言给出链接，**未能取回原文的一律标注**。

---

## 0. 读这份报告之前

**行号会过期。** 报告是在 `b7d4fe0` 附近写的，之后 `routines.py` 又被改过（加了 `Param` / `build`），所以里面
`routines.py:323-328` 这类引用已经漂了 —— 抽查过：`locomotion.py:367`、`:601`、`controller.py:264` 仍然准确，
`routines.py` 的那几处需要按**符号名**找（`_walk_seconds`、`_build_square`、`ROUTINES`），不要按行号。

**`docs/visual_editing_research.md` 是调研，不是计划。** 里面的建议没有一条被实现。第 1.6 节说的四个缺陷
**当时确实存在，而且现在仍然存在**（2026-10-14 逐条复核过），但报告里"每条都有测试能在当前代码上失败"
这个说法**不准确** —— 那些测试**不存在**，报告说的是"应该写这样的测试"。别把这句话当成已有覆盖。

**核心发现已独立复核两次**，一次由写报告的人、一次由我，用的是不同的方法（它读代码，我量工具返回耗时）：
`routines.py` 模块文档里"none of these actions reports completion"对三个最常用的原语是**错的**。
这条更正现在写在 `routines.py` 那句话旁边。

---

## 1. 实际在编排的是什么（从代码推出，不是从摘要）

### 1.1 数据模型

`Step`（`unitree_fleet_web/routines.py:106-139`）有五个字段：

```python
label: str          # 给人看的
tool: str           # 白名单里的一个工具名
args: Dict          # 该工具的固定参数
seconds: float      # 发完之后额外等多久
dangerous: bool     # 仅用于显示
policy: str         # all | any | best_effort
```

`Routine`（`:142-222`）是 `steps` 元组 + `loop` + `params` + `build`。

**关键之处：`Step` 没有 `robot_id`，也没有任何形式的目标字段。** 我 grep 过整个 `routines.py`，`robot_id` 一次都没出现。

### 1.2 执行

`fleet_action`（`unitree_fleet_web/service.py:1041-1105`）对每个已连接的狗并发调同一个工具：

```python
pairs = await asyncio.gather(*(one(rid) for rid in targets))
```

`_run_routine`（`service.py:948-1037`）按顺序遍历步骤，每步 `await self.fleet_action(...)`，然后**再**等 `step.seconds`（切 200 ms 片以便中止）。

所以两个维度是正交的：**步骤之间串行，步骤之内跨狗并行**。

### 1.3 "没有工具报告完成"——这句话只有一半是对的，而这一半的偏差是本报告最重要的发现

`routines.py:24-28` 的模块文档说：

> It uses declared durations rather than waiting for the robot, because *none of these actions reports completion*

**这不是事实。** 三个最常用的原语已经是阻塞的、闭环的：

| 工具 | 是否阻塞到动作结束 | 证据 |
|---|---|---|
| `robot_turn` | **是** — IMU 偏航角闭环比例控制，跑到容差或超时 | `unitree_robot_agent/tools/locomotion.py:367-380` → `motion/controller.py:264-360`，`await self.motion.turn(...)` |
| `robot_walk` | **是** — 里程计闭环，跑到 `arrival` 比例或超时 | `locomotion.py:428-433` → `motion/controller.py:367-470` |
| `robot_move` | **是** — `_go2_move` 自己 `await asyncio.sleep(duration)` 然后 `safe_stop` | `locomotion.py:601-631` |
| `robot_play_action`（运动类动作） | **否** — 发完 api_id 就返回 | `tools/actions.py:35-91` |
| `robot_stand` / `robot_lie_down` | **否**（同类运动指令） | — |

因为 `fleet_action` 会 `await` 工具的返回值，**阻塞类工具返回时动作已经做完了，`step.seconds` 于是变成纯粹的静默等待。**

这一点的算术后果是可以直接算出来的。`_build_square`（`routines.py:345-377`）默认参数下每一步是：

```python
Step("前进",     "robot_walk", {"meters": 0.4}, _walk_seconds(0.4))  # = max(2.0, 1.6) = 2.0
Step("右转 90°", "robot_turn", {"degrees": -90}, 6.0)
```

`_walk_seconds`（`:323-328`）自己说这是一个**上限而非时长**：

> `robot_walk` closes its loop and stops when it has measured the distance, so this is a **ceiling** on the wait rather than a duration

于是"走正方形"每走一条边，狗走完之后**原地站 2 秒**；每转一个 90°，转完之后**原地站 6 秒**。默认 9 步的声明总时长是 `2 + 4×(2+6) = 34 秒`，按钮上写着"9步/34s"——而真实耗时是**这个数加上四次行走和四次转身本身的闭环时间**。也就是说：**声明的时长既不是动作时长，也不是总时长；它是叠在一个耗时未知的阻塞调用之上的、额外的空等。** 按钮上的"约 34 秒"是一个偏短的承诺。

这就是"声明时长"这个设计真实的形状：**它不是模型的缺陷，是模型与工具语义之间一条从未被画出来的接线。** UI 从来没有告诉过操作员"这一步的工具会自己等"。

### 1.4 已经建立的规则（必须保留）

这些在源码里有明确论证，改动时不能丢：

- **时长是声明的、并且开始前就显示给操作员**（`routines.py:17-19`）——"a routine is a commitment of time as well as motion"。
- **策略存在是因为 `sum(results) > 0` 会静默地把双狗降级成单狗**（`routines.py:99-103`）。
- **每一步走同一套受守卫的工具**（`routines.py:20-22`），`confirm`、危险动作闸门、safe mode、审计全部照旧。
- **参数→步骤的算术在服务端**（`service.py:652-684`），理由是"在页面里再算一遍是同一事实的两份实现，它们会漂移"。
- **步骤失败不中止**——`_run_routine` 只把 `record["state"]` 设成 `failed`，然后继续下一步（`service.py:977-993`）。这是一个**没有对应选项的隐含决定**（见 §4.4）。

### 1.5 现状编辑器能表达什么（这才是"初级"的准确含义）

`renderEditor`（`static/console.js:1763-1863`）为每一行创建的元素，我逐个数过：

- 一个 `input`，标题"这一步之后等多久（秒）"；
- 一个 `select`，标题"这一步要几只狗做到才算成功"。

**就这两个。** 没有第三个输入。步骤的 `args` 在编辑器里**完全不可编辑**——`loadIntoEditor`（`:1975-1999`）只保留 `label/tool/args/policy/seconds` 五个键，`editorDocument`（`:1929-1948`）原样送回。参数是调色板按钮上烤死的。

调色板本身（`loadPalette`，`:1717-1761`）由两个来源拼成：舰队动作栏的 `posture` + `named` 两组，加上每狗控制里 `kind === 'button'` 且非 `read` 的项。后果：

- **`robot_walk` 到不了调色板。** `controls.py` 里 `robot_walk` 出现 0 次（`robot_move` 7 次）。也就是说**操作员无法在编辑器里写"前进 0.4 米"这个动作，而两条内置编排全靠它**。内置的 `走正方形` 用了 `robot_walk`，操作员只能复制一个已有的自定义编排才够得到。
- 反向地，`_GO2_HARD_ACTIONS`（13 个，`controls.py:213-227`）里那些 `robot_play_action` 步骤**可以**经调色板进入序列，而它们正是舰队动作栏里默认折叠的那一组。

### 1.6 四个已确认的缺陷

我把它们单列，因为它们是"更好的 UI 会暴露什么"的具体答案，每一个都能写出一条在改动前失败的测试。

**(1) `dangerous` 标志在编辑器往返中丢失。** 我实跑验证过：

```
内置 一起跳舞 的 dangerous 标志: [('站好',False), ('跳舞 1',True), ('跳舞 2',True), ('比心',False), ...]
经过编辑器往返（loadIntoEditor → editorDocument → validate）后: needs_dangerous = False
```

`validate` 读 `raw.get("dangerous", False)`（`routines.py:285`），而编辑器从不发送该键。**后果仅限于显示**（"（含需要危险动作许可的步骤）"这句提示消失，`console.js:2825`）——真正的闸门在工具内部（`tools/actions.py:52` 的 `require_dangerous_allowed`），走的是 profile 自己的 `dangerous` 标志，不是 `Step` 的。所以这**不是安全漏洞，是一句丢在路上的告示**——正好是 `tests/test_console_payload_contract.py` 存在的理由那一类。

**(2) 内置编排无法载入编辑器，尽管注释说可以。** `console.js:1972-1973` 写着：

> A built-in can be opened too, and saving it under a new name is the intended way to say "like 走正方形 but with three laps on a different day"

但 `loadIntoEditor` 全文件只有两个出现位置：定义处（`:1975`）和 `loadCustomRoutines` 里的"编辑"按钮（`:2065`）。`routineButton`（`:2809-2843`，内置编排的那个）只挂了"运行"和"参数"。**这条路径在界面上不存在。**

**(3) 自定义编排不可能有参数。** `validate` 里 `params` 和 `build` 两个词一次都没有出现（我用脚本确认过）。`validate` 返回的 `Routine` 永远 `params=()`、`build=None`。只有两条内置编排有旋钮。

**(4) 横向卡片条。** `.timeline { display:flex; overflow-x:auto }`、`.step-card { flex:0 0 auto; min-width:96px }`（`static/console.css:858-884`）。40 步 ≈ 4000 px 横向滚动，且纵向空间全部浪费。

---

## 2. 形状对比

对每种形状，我关心的是**它擅长什么**和**它把什么弄得很别扭**，以及**对本问题的适配判决**。

| 形状 | 擅长 | 别扭 | 判决 |
|---|---|---|---|
| **节点图 / 数据流**<br>Blueprints、Blender 节点、Node-RED、n8n、ComfyUI | 扇入扇出、真正的并行分支、数据变换管线、可视化中间结果 | **严格的线性序列**：没有分支可画，画布上全是连线绕行（edge routing），顺序靠箭头而不是靠位置表达；**没有时间轴**——x 轴是拓扑不是时间，所以"同时发生"和"等 3 秒"都无处安放；40 个节点的图需要缩放和平移才能读 | **不适配。** 见 §4.1 |
| **积木块 / 块编辑器**<br>Scratch、Blockly、MakeCode、Snap! | 嵌套结构一眼可读、语法错误在结构上不可能、对新手友好、块形状本身传达可插入位置 | 拖拽是主要交互，**键盘/触摸的可达性一直是公认的难题**（Blockly 官方有专门的无障碍计划，说明这不是免费得到的——*该页重定向至 docs.blockly.com，我未能取回原文，标注为 UNVERIFIED*）；自上而下的单列与"步骤列表"信息量相同，只是更重；仍然没有时间轴 | **形状对了一半。** 值得借鉴的是"块形状传达约束"，不值得照搬的是拖拽为主的交互。本项目的既定口味（不用图标-only 按钮、拖拽只作为上下移的补充，`console.js:1778-1783`）已经做出了正确判断 |
| **时间线 / 音序器**<br>Premiere、Ableton、钢琴卷帘、MotionBuilder、NLA | **时间就是 x 轴**：时长靠拉伸、同时性靠纵向堆叠、播放头、缩放；多轨天然表达"多个主体同时进行" | 单条事件的**语义不可读**——"这一块是什么"要去检查器里看；长序列必须缩放；对"没有分支的线性表"来说，轨道布局是过量的表达力 | **适配度最高。** 但它有一个前提：轨道要有意义。我们只有两条狗、没有可画的连续量，所以直接抄 NLE 会得到一个空的轨道区。见 §4.2 的裁剪 |
| **状态图 / 状态机**<br>Stately/XState、UML、Unity Animator | 反应式系统、模式与守卫、事件驱动的迁移、可视化解锁条件 | **本问题没有事件可反应。** 没有 completion 信号、没有传感器条件、没有外部触发器，状态机就退化成一条直线，"状态"和"步骤"变成同一个东西；守卫无处可写 | **不适配。** 它买的是反应性，而这里恰恰缺的是反应性的输入 |
| **DAG 编排器**<br>Airflow、Celery canvas、GitHub Actions、Prefect、Temporal | 依赖图、扇出重试、失败分支、可观测性、幂等 | 当图是一条链时，它就是一个**多了一层配置的列表**；Airflow 自己的最佳实践文档有专章 "Reducing DAG complexity"，承认复杂度是要主动压制的开销 | **不适配。** 而且它假定了我们最缺的东西：任务的完成可观测 |
| **智能家居自动化**<br>Home Assistant、IFTTT、Shortcuts | 触发器→条件→动作的三段式、嵌套块、`delay` 这个"等一段时间"的积木、`run in parallel` 块 | HA 的编辑器是**逐块下钻的表单**，看全局要滚动很长的树；它原生面向"一个设备响应一个事件"，不是"两台设备按时间表同步动作" | **部分适配。** `Wait for time to pass (delay)` 和 `Run in parallel` 正是本问题的两个积木；但整体形状对多主体时间表太啰嗦 |
| **表 / 追踪器 / 提示单**<br>QLab、SFC、Renoise/LSDJ、剧场 cue sheet、电子表格 | **一行一步，纵向可扫描**；密集；一屏能看很多步；列可以承载"谁/多久/失败怎么办"这些正交属性；40 行是一个完全正常的文档长度 | 表达嵌套和分支很笨拙；时间比例不可见（第 3 步比第 4 步长多少，要看数字而不是看长度） | **这是正确的形状。** 见 §4 |
| **实时编码**<br>TidalCycles、Sonic Pi | 循环与复调极紧凑；改一行立刻听到 | 面向程序员；对"操作员"这个角色不成立 | **不适配** |

**跨形状的两个横切结论：**

1. **凡是假定被驱动对象会报告完成的形状，在"发完即忘"的世界里都会缺一块。** 数据流图（等上游产出）、状态机（等事件）、DAG（等任务成功）都属于这一类。**只有提示单/时间线这一族，以及 SFC 的"时间型迁移"，天生处理"我声明等 5 秒，因为没人会告诉我什么时候完"。** QLab 把这件事写成了产品术语，而且措辞精确到可以直接引用：

   > **Auto-continue** happens after a given post-wait time… Pressing GO will play cue 12 immediately, wait 3 seconds, and then play cue 13, **and it doesn't matter whether cue 12 has finished yet.**
   > — [QLab 3, Cue Sequences](https://qlab.app/docs/v3/general/cue-sequences/)

   `auto-follow`（按**上一条的时长**触发下一条）与 `auto-continue`（按**post-wait** 触发，不管上一条完没完）的这一对区分，正是 §1.3 那个问题的现成答案。

2. **"N 个主体同时做一件事"只有两种形状能一眼读懂：时间线的纵向堆叠，和表格里的一列。** 节点图要靠共享前驱来暗示，状态机要靠并行区域（一个很少被使用的 UML 特性）。

---

## 3. 机器人编排的一手先例

### 3.1 Boston Dynamics Choreographer —— 时间线，而且已经踩过我们的每一个坑

[Choreographer Developer Guide](https://dev.bostondynamics.com/docs/concepts/choreography/choreographer) 是我找到的最贴近的先例。它的形状：

- **时间线 + 轨道（tracks）**：Legs、Body、Arm、Gripper、Lights、AV Lights、AV Buzzer、Annotations、Music。
- **切片 / 拍 / BPM**：时间被切成 slice（4/4 拍下等于十六分音符），每个动作占整数个 slice。**时长可以靠拖拽块的边缘拉伸**，动作自己会强制它自己的最小时长。
- **同时性有一条明确定义的规则**：

  > Moves that control **any of the same tracks** cannot run simultaneously. Moves that use **different tracks** can be run simultaneously.

  这是"什么可以同时发生"的**资源模型**——不是靠画布上摆得下，而是靠轨道不冲突。这一条对我们直接适用：一条狗不能同时做两件事。
- **多机器人**：靠一张 robot management 表，每个连接一行，行里有一个 "Dance Selection Column" 选择该狗执行哪个打开的编排，加 checkbox 决定是否接收命令。**多机器人不是画布上的多个泳道，而是"同一个时间线 × 一份分配表"。**
- **Beginner / Advanced 模式**：Beginner 缩小参数范围并移除部分高动态动作，默认开启。原文的理由值得逐字引用：

  > Choreographer provides as much design freedom as possible. **A consequence of this freedom is that the robot cannot perform some combinations of moves, parameters, and other settings under all conditions.**

  这就是用户问的"更强大的编辑器会不会让操作员编出在硬件上失败的东西"的**厂商级确认**——而且 BD 的应对不是限制表达力，是**给参数范围分档**。
- **它还要求操作员自己验证参数**：

  > Be sure to test and make sure the robot can handle your parameters! … if a combination of parameters does not work, adjust them until they do for your situation.

- **时间码**（[Timecode in Choreographer](https://dev.bostondynamics.com/docs/concepts/choreography/timecode_reference)）：支持 SMPTE LTC 作为**外部主时钟**，让 Spot 与灯光、音乐同步。关键细节是**倒计时是随命令下发到机器人的**：

  > At the set start time, robots begin dancing. This is triggered by **the robot countdown communicated by Choreographer** at step 4, not by a live timecode signal.

  也就是说连 BD 也不靠"持续推进的时间码"来驱动机器人，而是**预先下发一个起跑时刻**。这是多主体同步的通用解法，对我们暂时不需要（一次只有一场、没有外部设备），但值得记着：如果将来要对音乐，正确的做法是下发起跑时刻而不是逐帧同步。
- 还有一条安全警告与我们高度相关：

  > **robots running choreography actions are not capable of reacting to their surroundings, and will hit obstacles if they are in the path** … Missions with Choreography Actions should not run unsupervised.

  "编排中的机器人不会对周围作出反应"——这正是本项目的编排在真机上的语义，而它**没有出现在 `routines.py` 的任何注释里**。

**Spot Autowalk / 录制回放**（[Choreography in Autowalk](https://dev.bostondynamics.com/docs/concepts/choreography/choreography_in_autowalk)、[Edit Autowalk](https://dev.bostondynamics.com/python/examples/edit_autowalk/readme)）：形状是**示教-回放**——人带着狗走一遍录下路线，然后在路径点上插入 `Execute Choreography Action`。录制出的任务文件会**把用到的编排和动画一起打包**，所以换一台机器人不需要预先传文件。对我们的启示有两条：(a) 示教-回放适合**路径**，不适合**动作**——我们并没有路径可教（两台狗在同一个房间里各走各的，没有共享坐标系，见 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) A1）；(b) "任务文件自带依赖"是一个好性质，我们的 `routines.json` 已经做到了。

### 3.2 无人机编队 —— 编辑器里内置安全检查，这是最强的先例

[Skybrush Studio for Blender](https://docs.skybrush.io/public/skybrush-studio-for-blender/latest/tutorials/easy-drone-show-design.html) 的形状对我们最有参考价值，因为它把"编辑"和"验证"放在同一个循环里：

- **Storyboard = 有序条目列表**，每条 = 一个 formation + start frame + duration（[Storyboard panel](https://docs.skybrush.io/public/skybrush-studio-for-blender/latest/panels/formations/storyboard.html)）。条目可以有 `Purpose`（Takeoff / Show / Land / Unspecified）——**这正是"分组"的一个已落地形态**：分类不是为了好看，是在现场适配时决定"哪些保留、哪些按实际环境替换"。
- **过渡有两种时间调度**：`Synchronized`（所有无人机同时出发同时到达）与 `Staggered`（带 departure/arrival delay 的错峰）。**"同时"与"错峰"是一个显式的、每段过渡的选择**，这是我们的模型里完全没有的维度。
- **迁移有速度剖面**：Linear / Smooth / Smooth from left / Smooth from right。**迁移不是瞬移，它占时间是可见的。**
- **[Safety Check panel](https://docs.skybrush.io/public/skybrush-studio-for-blender/latest/panels/safety_and_export/safety_check.html)**：实时检查 proximity / altitude / velocity / acceleration / yaw rate，违规的无人机在 3D 视图里按违规类型染色，并有 `Validate Trajectories` 做全时间范围校验，输出到 Skybrush Viewer 的图表。**并且它诚实地说明自己不知道什么**：

  > Skybrush Studio is **unaware of the physical dimensions of the drones**.

- **导出的是一个经过验证的编译产物**（`.skyc`），还能导出 **validation PDF** 用于申请飞行许可。

**这里最值得抄的不是任何一个面板，而是那个结构：编辑 → 实时静态检查 → 显式校验 → 编译产物。** 对我们可以落地的对应物见 §4.5。

- **开演时间**（[Choose start time](https://docs.skybrush.io/public/skybrush-live-doc/latest/tutorials/show-in-10-steps/choose_start_time.html)）：起跑时刻可设本地时间或 SMPTE 时间码；起演方式可以是遥控器手动（"更安全，但你要手动对齐时钟"）或 Live 按钟自动。还有一个直白的告警：**室外演出时电脑时钟必须很准，因为无人机靠 GPS 精确对时**——时钟不同步，演出就不会在显示的钟点开始。

- 事故先例（**未取回原文，标注 UNVERIFIED**）：搜索命中了澳大利亚 ATSB 关于 Docklands 无人机编队事故的通报（[atsb.gov.au](https://www.atsb.gov.au/news/2025/drone-swarm-accident-highlights-importance-system-knowledge-active-alerting)，取回返回 HTTP 403）与 Orlando Lake Eola 事故的 NTSB 报道（[fox35orlando.com](https://www.fox35orlando.com/news/orlando-drone-show-errors-led-lake-eola-crash-seriously-injured-7-year-old-ntsb)）。按标题，两者都指向**操作/系统知识与人因**而非单纯硬件失效。我未能读到报告正文，因此不作为论据使用。

### 3.3 ROS 2 行为树 —— 有编辑器，但买的不是我们需要的东西

[BehaviorTree.CPP](https://www.behaviortree.dev/) + [Groot2](https://www.behaviortree.dev/groot/)：

- 树用 **XML 定义**，编辑器拖拽生成 XML，同时**实时预览 XML**——"图形是 XML 的视图，XML 是真相"。这与本项目"参数→步骤的算术在服务端"是同一种口味。
- 有实时监视、日志回放、断点、**故障注入**（PRO）。
- 价格是一个有用的成本坐标：**Groot2 PRO €590/年（浮动许可）**，免费版限制监视与日志可视化的节点数。
- Nav2 的 [Groot2 教程](https://docs.nav2.org/rolling/tutorials/general_tutorials/groot_tutorials/groot2/groot2/) 我取回的正文很薄，只确认了它是 Nav2 官方推荐的编辑/调试路径（*UNVERIFIED：具体交互细节*）。
- **FlexBE**（[flexbe.readthedocs.io](https://flexbe.readthedocs.io/en/latest/)）是 ROS 生态里的层级状态机编写工具，带 WebUI（*我未取回原文，UNVERIFIED*）。

**判决**：行为树擅长的是"反应式、可复用、可组合、能在运行时被打断重规划"的行为。我们的编排**没有反应输入**——没有传感器条件、没有失败分支、没有重试语义。用行为树表达一条直线，得到的是 `Sequence` 节点下面挂 40 个叶子的 XML。**它买的是我们不需要的模块化，代价是我们需要的紧凑性。**

### 3.4 工业顺序控制 —— SFC / GRAFCET：问题同构，解法已成标准

IEC 61131-3 的 **Sequential Function Chart**（及其前身/表亲 **GRAFCET**，IEC 60848）是**为"步骤序列 + 迁移条件 + 动作"这个形状制定的国际标准**，用在 PLC 上。它的基本语法是 step-transition-step 严格交替，每个 step 可以挂**零个、一个或多个动作**（PLCopen 的 SFC 文本材料，*该 PDF 我未能取回正文，此句来自搜索片段，标注 UNVERIFIED*——[plcopen.org 下载页](https://www.plcopen.org/download_file/force/5f66721e-9586-43d0-9f7c-79faa00e9ae4/342/)）。

**关键的一点：迁移条件可以是时间。** 当没有任何传感器能报告"这一步做完了"，SFC 的答案不是猜一个 `sleep`，而是把**定时器写成迁移条件**——"激活后 t#5s 迁移"。这是同一个问题的标准答案，而且是四十年前就标准化的。动作限定符（action qualifier）区分了 N（非存储）、S/R（置位/复位）、**D（时间延迟）**、**SD（存储且时间延迟）**等语义（*限定符表出自搜索命中的 [TU Graz 出版物](https://repository.tugraz.at/publications/marc21/6gd3t-4wd63/files/75179.pdf?download=1)，我未取回原文，UNVERIFIED*）。

**GRAFCET 有一个被文献充分记录的问题，而它对我们是一个警示。** [Mroß et al., *Unambiguous Interpretation of IEC 60848 GRAFCET based on a Literature Review* (arXiv:2307.11556)](https://ar5iv.labs.arxiv.org/html/2307.11556) 系统梳理了这个标准里的**语义歧义**：初始步是否已激活、输入事件能否同时发生、边沿事件在一次瞬态演化中保持多久、宏步嵌套、强制命令（forcing order）的层级优先级……文中一句总结很值得引：

> Because of the semiformal nature of the standard, an unambiguous definition of GRAFCET is not given and different authors have interpreted its ambiguities in different ways.

以及关于"迁移是零时间的、而 PLC 是循环扫描的"这一根本错配：

> In GRAFCET, evolutions require no time … The execution mode of PLCs follows the cyclic behavior of reading inputs, executing code and writing outputs. All of these parts take longer than zero time, resulting in a discrepancy between the specification and the implementation.

**对我们的教训**：**一个"步骤 + 迁移"的语言，只要迁移条件的语义不精确，就会在实现层面分裂成多种行为。** 我们现在的 `seconds` 字段恰好语义不清（§1.3：它到底是"动作时长"还是"额外空等"？），而它已经产生了可以算出来的浪费。**把 `seconds` 的语义写清楚，比给它加任何可视化都重要。**

### 3.5 主题公园 / 演出控制 —— "show control = 提示单 + 主时钟"

- **QLab**（[Cue Sequences](https://qlab.app/docs/v3/general/cue-sequences/)）：`auto-follow`（按上一条时长）、`auto-continue`（按 post-wait，**不管上一条完没完**）、`pre-wait`（触发后先等再开始）、`start-all group`（同时起）。文档明确点出 `pre-wait` + `start-all` 的用法：

  > A pre-wait is a valuable tool to use in conjunction with start-all Groups as a way to **create a timeline of sorts**.

  即：**一个纯列表 + 两个时间字段，就能表达出一条时间线。** 这对我们很重要，因为它意味着我们不需要真的去实现拖拽时间线控件。
- **Timecode**（[Using Timecode To Control QLab](https://qlab.app/docs/v4/control/using-timecode-to-control-qlab/)）：QLab 的文档里有一段罕见的、诚实的反对意见，我几乎想整段抄进 `routines.py`：

  > Timecode is a bit at odds with the design premise of QLab, which is that **you don't necessarily know the amount of time that will elapse between cues. After all, that's the whole point of having cues in the first place!** Using timecode locks a series of events in place. Obviously, that's sometimes quite useful.

  **"提示单的存在意义就是你不必提前知道下一条什么时候开始"**——这对我们目前*不*成立（我们要的是可重复的演出），但它点出了一个真实的取舍：**一旦时长全部声明死，编排就失去对现场情况的适应性。** 我们的模型里唯一的适应机制是 `policy`，而它只影响计不计失败，不影响时序。

### 3.6 示教-回放 / 记录重放

- UR **PolyScope X**（[First Program](https://www.universal-robots.com/manuals/EN/HTML/SW10_12/Content/prod-usr-man/software/PolyScopeX/polyx-initialize/polyx-First-Program.htm)）我取回了正文，它给出的形状是**程序树（program tree）里的线性命令节点**。官方教程那第一个程序，逐字是：

  > (1) move to position A using a joint move type, (2) set an IO, (3) **wait for 3 seconds**, (4) unset the IO, (5) move to position B …, and (6) repeat the movement.

  **一个工业机器人的示教盒，"第一个程序"的六步里第三步就是"等 3 秒"**，并且 `Wait` 命令有一个 `Wait Type` 字段，`Time` 是其中一个取值（原文：`In the Wait Type field, select Time` / `enter 3 (three seconds)`）。这与 §3.4 的 SFC 时间型迁移、§3.5 的 QLab post-wait 是同一件事，出现在三个完全不同的领域里。

  另外两点值得记：`Move to` 命令把 Waypoint **合并**进去了（原文：`in PolyScope X, the Waypoint node is merged to the Move to command`）——"移动"和"移动到哪"是同一个节点，不是一个节点的参数；以及程序树里执行到哪一步是**用绿色标出来的**，和我们的 `.step-card.running` 是同一个想法。
  （[PolyScope X 手册 PDF](https://www.universal-robots.com/manuals/EN/PDF/SW10_10/PolyX_handbook_online/PolyScope%20X%20Handbook_en_Global.pdf) 我未取回正文。）
- **判决**：示教-回放解决的是"**几何**很难用数字描述"这个问题（把机械臂掰到位置，比写六个关节角容易）。我们的问题里**几何只占很小一部分**——`robot_walk(0.4 m)` 和 `robot_turn(-90°)` 用数字描述毫无困难。**所以示教-回放对本项目的价值很低**，除了一个可能的例外：如果将来要让操作员"录一遍我手动遥控的动作然后重放"，那是另一条产品线，不是编排编辑器。

### 3.7 先例的横向结论

| 工具 | 形状 | 多机器人怎么表达 | 无反馈怎么办 |
|---|---|---|---|
| BD Choreographer | 时间线 × 轨道 | 时间线 × 每狗一行分配表 | 动作时长是**动作自带的**（block 宽度）；BPM 决定绝对时间 |
| Spot Autowalk | 录制的路径 + 插入的动作 | 一狗一个任务 | 动作自行结束 |
| Skybrush | storyboard 列表 + 时长 | 一个 storyboard，全体无人机 | 轨迹是**预计算的**，机上按自己的时钟跑；起跑时刻下发 |
| Groot2 / BT.CPP | 树 | 每个机器人一棵树 | ticking 模型，天然不需要 completion |
| SFC / GRAFCET | step-transition | 每个 PLC 一份程序 | **时间型迁移条件** |
| QLab | 提示单 | 每个 cue 指向自己的目标 | **post-wait / pre-wait**，文档明说"不管上一条完没完" |
| UR PolyScope X | 程序树里的线性命令节点 | 一台机器人一份程序 | **显式的 `Wait` 命令**（`Wait Type = Time`） |

**没有一个先例用节点图。** 最接近我们问题的两个（Choreographer、SFC）用的是时间线和 step-transition，而"发完即忘 + 声明等待"的最佳术语来自 QLab 的提示单。

**而"等一个声明时长"这件事，在四个互不相干的领域里各自被独立发明了一遍**：SFC 的时间型迁移、QLab 的 post-wait、PolyScope 的 `Wait` 命令、Choreographer 的 block 宽度。**这不是一个需要我们重新设计的机制。**

---

## 4. 建议

### 4.0 一句话

> **形状保持线性，把横向卡片条换成纵向的"运行单"（run sheet）：一行一步，加上"谁 / 等多久 / 失败怎么办"三列，加上"段落"这一种非命令行，加一个只有静态检查能力的页脚。不做节点图，不做拖拽时间线，不做分支。**

用户要的"可视化编程"，在这里正确的解读是 **"可读的、有结构的、能一眼看到代价的运行单"**，而不是"可连线的图"。

### 4.1 为什么节点图在这里是错的工具

直说：

1. **没有分支可画。** 一条 40 步的线性序列画成节点图就是 40 个节点 39 条边，全是直线。连线不承载任何信息——顺序完全由位置决定，所以连线是纯成本（绕行、交叉、布局算法）。
2. **没有数据流。** 每一步的 `args` 是常量，步骤之间不传递任何值。数据流图的核心表达力（A 的输出是 B 的输入）在这里没有对应物。
3. **它会把时间藏起来。** 这是最严重的一条。这个问题的核心事实是**时间**：一共多久、这一步等多久、两条狗是不是同时。节点图的 x 轴是拓扑深度，不是时间。**你会花一个月做一个界面，然后把操作员唯一真正需要看的那一维弄丢。**
4. **同时性在节点图里是隐含的。** "这一步发给两条狗"在图上只能靠共享前驱暗示；在表格里就是"谁"那一列画两个点。

**唯一能支持节点图的论据**是"将来加分支"。我的看法：**先有分支需求，再有分支 UI。** 现在没有任何一条已表达的需求需要分支（`policy` 不是分支，它是计数）。如果将来真的要"dog1 做成了才做下一步"，那需要的也不是节点图，而是 SFC 的**迁移条件**——那是 step-transition 形状，不是 dataflow 形状。

### 4.2 建议的 UI

```
┌─ 调色板 ──────────┬─ 运行单 ─────────────────────────────────────────────┐
│ 姿态              │  #   做什么          谁     等多久    失败            │
│ [站立][趴下][恢复]│ ─────────────────────────────────────────────────────│
│ 动作              │ ▸ 开场                                               │
│ [打招呼][鞠躬]    │  1   站好            ● ●    2.0 s    全部   ↑↓ ✕    │
│ [比心][扭屁股]    │  2   打招呼          ● ●    3.0 s    全部   ↑↓ ✕    │
│ 移动              │ ▸ 高潮                                               │
│ [前进…][左转 30°] │  3   前进 0.4 m      ● ●    auto ◂    全部   ↑↓ ✕    │
│ [右转 30°][前进…] │  4   右转 90°        ● ●    auto ◂    全部   ↑↓ ✕    │
│ 危险              │  5   比心            ● ●    3.0 s    尽力   ↑↓ ✕    │
│ ⚠ [前空翻][后空翻]│ ▸ 收尾                                               │
│                   │  6   趴下            ● ●    2.0 s    全部   ↑↓ ✕    │
│                   │ ─────────────────────────────────────────────────────│
│                   │ 6 步 · 声明 10 秒 + 2 个动作的实际耗时               │
│                   │ ◂ robot_walk / robot_turn 自己会等到动作结束         │
│                   │ ⚠ 1 步需危险动作许可 · 需 3 m 净空 · 发给全部 2 只狗  │
└───────────────────┴──────────────────────────────────────────────────────┘
```

具体到像素和字段：

**(a) 纵向列表，不是横向条。** `.timeline` 从 `flex-direction: row` 改成 `column`，`min-width: 96px` 去掉，`.step-card` 变成 `display: grid` 的行。40 步就是一个 40 行的文档——**这是文档该有的长度**，滚动是正确的手势。纵向还顺带让每一行有位置写更多列。

**(b) `等多久` 列有两种值，这是本建议里最重要的一个功能。** 值来自服务端的一份新表——**"这个工具是否阻塞到动作结束"**：

```python
# 拟：routines.py
#: Tools that return only after the motion has finished.
#:
#: `robot_turn` runs a proportional loop on the IMU yaw and `robot_walk`
#: closes the loop on odometry, so by the time either returns the robot has
#: stopped. Waiting the declared `seconds` on top of that is standing still
#: for no reason — which is what a `走正方形` step does today, four times per
#: lap, for 2 s after every walk and 6 s after every turn.
BLOCKING_TOOLS = frozenset({"robot_turn", "robot_walk", "robot_move"})
```

- 工具在 `BLOCKING_TOOLS` 里 → 该列默认显示 **`auto`**（"这一步会自己等自己"），操作员可以改成一个数字来**加**一个有意为之的停顿。
- 不在表里 → 显示数字，默认 `3.0`，这是真正的声明时长。
- 数据模型上 `seconds: float` 变成 `seconds: Optional[float]`，`None` = auto；`validate` 把缺省值按 `tool` 决定，而不是一律 3.0。
- **页脚的总时长必须诚实**，这是最容易做错的一处。对 `auto` 的步骤不能算 0（那就把 §1.3 的错误从步骤级搬到编排级），也不能算一个「大概 3 秒」（那是编造）。**显示成「声明 10 秒 + 2 个动作的实际耗时」**——把已知的和未知的分开说，而不是合成一个看上去精确的数字。这也是 Skybrush 那条"我们不知道无人机的物理尺寸"的同一种诚实。

**(c) `谁` 列。** v1 只画点：所有已连接的狗各一个点，和现在 `renderTimeline` 的 per-dog lane 一样（`console.js:3087-3103`）。**v1 不可编辑**——见 §4.3。

**(d) `失败` 列就是现在的 policy 下拉。** 保留。它的存在理由（`routines.py:99-103`）已经论证过，而且它在表格里比在卡片上更好读。

**(e) 段落行 = 一种不是命令的步骤。** 数据模型里就是一个 `Step`，`tool` 为空、新增 `kind: "section"`：

```python
Step(label="高潮", tool="", kind="section")
```

三点好处：runner 遇到 section 跳过（不发命令、不等待）；`validate` 不把它计入 40 步上限；`renderTimeline` 和 `renderEditor` 用同一个渲染器，所以**编辑器里看到的段落，运行时就长在同一个位置**。这是"编辑器和运行显示应该是同一个视图"的第一步——今天它们是两个不同的东西（一个是 `<ol class="editor-steps">`，一个是 `<ol class="timeline">`），这没有道理。

**(f) 页脚 = 静态检查，诚实地说明它不知道什么。** 这是从 Skybrush 的 Safety Check 抄来的结构，缩到我们**离线真正能知道**的范围内：

```
6 步 · 约 14 秒 · ⚠ 1 步需要危险动作许可 · 需要 3 m 净空 · 全部步骤同时作用于 2 只狗
```

能算的：步数、声明时长（诚实表述，见 (b)）、是否有 `dangerous` 步骤、是否需要净空（`_GO2_NEEDS_SPACE`，`controls.py:233-236`）、是否**所有步骤都发给全部狗**（v1 恒真，等 §4.3 落地后变成一条真实的信息）、每一步的 tool 是否仍在 `CONSOLE_TOOLS` 里且当前已连接狗都支持。

**明确不能算的**（页脚要写出来，或者至少代码注释要写）：**碰撞**。没有共享坐标系、没有可靠的里程计（[OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) A1 说得很清楚：那只 Go2 从不发布 `rt/sportmodestate`），`sync.py` 的整个设计前提就是"只能测两只狗**动得有多不一样**，不能测它们在哪"。**在这样的输入上做碰撞检查，就是在编造一个关系。** Skybrush 能画接近告警是因为它有每架无人机的绝对位置和预计算轨迹；我们没有。页脚应该说"需要 X 米净空"这类**静态**的话，而不是"两只狗将在 3.2 秒时相距 0.4 米"这类**看起来精确**的话。

### 4.3 数据模型改什么、不改什么

**v1 只加一个字段**：`Step.kind`（缺省 `"command"`，另一个值是 `"section"`），以及把 `seconds` 变成可空。**其余一切不动**，`as_dict` 的形状、`validate` 的白名单检查、`FleetAction` 的路径、`policy` 的语义，全部保持。

理由是项目的既定口味：**"parameterised version is the same routine with different numbers, not a different kind of object"**（`routines.py:153-157`）。段落行和 `auto` 等待也应该是"同一个对象的另一种取值"，不是新的对象类型。

**v1 明确不做：per-dog 目标。** 这是用户列的候选 (a)，我同意它是最有价值的模型改动——服务器端其实不难（`fleet_action` 里 `one(rid)` 这个闭包已经是按狗的了，`service.py:1077-1090`）。**但它也是把"能编出的东西"扩张得最厉害的一项，而扩张的方向正对着会撞在一起的那个方向。** 两台狗在同一个房间里各做各的，是从"一个意图"变成"两个意图"，从"编排"变成"多智能体调度"。今天的安全叙述（"每一步走同一套受守卫的工具"）恰好成立，是因为**每一步是一个意图**；一旦一条狗在鞠躬、另一条在走，就没有任何东西在管它们之间的距离了。

所以：**v2，而且必须带一个 UI 上的不对称告警**（"这一步之后，两只狗在做不同的事"），并且只在操作员已经标记过 `sync` 基准时才允许。**我会把这条写成建议而不是要求**——它是最可能让这个功能变成一个月的部分。

### 4.4 顺手修掉的四个具体缺陷

它们都能独立成一次提交，而且每一个都能写出一条在改动前失败的测试：

1. **`dangerous` 往返丢失**：`editorDocument`（`console.js:1943-1946`）把 `dangerous: step.dangerous` 加上，`loadIntoEditor`（`:1977-1983`）把它读回来。测试：把编辑器会送出的文档喂给 `validate`，断言 `needs_dangerous is True`——**在改前的代码上这是 `False`**（我在 §1.6 实跑过）。
2. **失败不中止**：给 `Routine` 加一个 `on_failure: "continue" | "stop"`，缺省 `continue`（保持今天的行为，因为改缺省就是改语义）。测试：`policy="all"` 且两只狗都失败时，`stop` 不再执行下一步——**今天的代码一定会继续执行**。
3. **调色板够不到 `robot_walk`**：给 `controls.py` 加一个 `robot_walk` 的描述符（`group="motion_nudge"` 或新的 `"motion_measured"`）。测试：断言 `/api/console/controls` 里有一个 `tool == "robot_walk"` 的项，或断言调色板条目里包含它——**今天两者都不成立**。
4. **内置编排载不进编辑器**：给 `routineButton` 加第二个按钮（"以此为模板"），复用已有的 `loadIntoEditor`（它已经正确处理了 `builtin` 分支，`:1986`）。测试：浏览器驱动里点内置编排的模板按钮，断言 `#editor-steps` 非空——**今天这个按钮不存在**。

第 1 和第 3 是 bug，第 2 和 4 是缺口。四个都不需要动形状。

### 4.5 它刻意不做什么

- **不做节点图。** 理由见 §4.1。
- **不做拖拽时间线。** 理由是 QLab 那条：`pre-wait` + 一个同时起的分组就已经是"a timeline of sorts"。而且拖拽时间线是一条无法用上下按钮替代的交互，会撞上项目"console 不该要求某一种输入设备"的既定立场（`console.js:1697-1700`）。
- **不做条件分支。** 没有 completion 信号、没有传感器条件可以写。SFC 的迁移条件在有 I/O 的地方是对的，在这里绝大多数迁移条件都会是"等 3 秒"——那就是 `seconds` 字段，只是穿了制服。
- **不做变量与表达式。** 唯一的参数化机制是服务端的 `params` + `build`（`routines.py:142-198`），它已经工作，而且原因充分。**顺带**：可以考虑让自定义编排也声明 params——但那需要把 `build` 从 Python 函数变成可序列化的东西（比如"重复该步骤 N 次"），是一个独立的、比 UI 大得多的问题。
- **不做碰撞检查。** 理由见 §4.2(f)。
- **不加新工具。** 词汇量的问题是真的（§5），但那是另一件事。

---

## 5. 对"瓶颈是编辑器还是词汇量"的回答

用户问得对：**"一个更好的编辑器，是不是只是让排列一小撮动作变得更容易，而这不值多少？"**

我的判断：**两者都是瓶颈，但它们在时间上不是同一个瓶颈。**

**词汇量是硬上限，而且它比看上去更小。** Go2 的动作目录是 22 项（`robots/go2.py:70-93`），其中 13 项是危险动作（`controls.py:213-227`），安全的那 9 项是打招呼、伸懒腰、满足、摆造型、鞠躬、扭屁股、比心、坐下、从坐姿站起——**这九件事在观感上是高度同质的短表演动作**，时长都在 3-8 秒。加上姿态（站/趴/恢复）和移动（走/转/横移），全部词汇大约 20 个，而且**大部分是"做一个 3 秒的姿势"**。

**这是一个真实的、不能靠 UI 解决的贫瘠。** 但有两件事缓解它，而两件都被现状压住了：

1. **同样的动作，两个主体的时序关系本身是表达力。** "一起鞠躬"和"dog1 先鞠躬、dog2 半秒后跟上"是两个不同的作品，而后者今天做不到（要等 §4.3 的 v2）。更便宜的是**对称的时序变化**：同一串动作，间隔不同、顺序不同、重复次数不同，观感差别很大——这些**今天全都做得到**，只是很难做，因为每改一个等待都要点进一个 46 px 宽的数字框，而你不知道那个等待是不是多余的。
2. **`seconds` 的语义不清在偷走表达力。** §1.3 的算术说明：`走正方形` 每一圈有 32 秒里的一大半是空等。**把 `auto` 做出来，等于凭空还给操作员十几秒的可编排时间。** 这不是美观问题，这是词汇量问题——那十几秒本来可以用来做动作。

所以我的排序是：**先把 `auto` 等待做出来（它直接增加可用时间，也直接暴露模型的语义），再做运行单和段落（它让 40 步可读，也让"做个长一点的节目"从不可能变成可能），最后才考虑 per-dog 和词汇量。** 反过来做（先堆动作）会得到一堆无法按时间组织起来的动作。

**一个必须说清楚的不确定性**：上面关于"观感同质"的判断来自读 `robots/go2.py` 的动作名和描述，**我从未在真机上看过这些动作**。如果这九个动作在实物上差异很大（比如"月球步"和"单手倒立"是完全不同的表演），那么词汇量的瓶颈比我估的轻，形状的瓶颈就更是主导。**这是这条判断最可能出错的地方，也是最容易验证的**——放一次真机，把九个安全动作连着做一遍看看。

---

## 6. 成本与风险

### 6.1 分档

| 工作 | 量级 | 改动面 | 可否独立发布 |
|---|---|---|---|
| **`dangerous` 往返修复** | 数小时 | `console.js` 两处 + 一条测试 | ✅ 完全独立，且是 bug 修复 |
| **调色板补 `robot_walk` / `robot_turn`** | 半天 | `controls.py` 加描述符 + `test_console_controls.py` 的镜像表 + 一条测试 | ✅ 独立 |
| **内置编排"以此为模板"按钮** | 半天 | `console.js` 一个按钮 + 浏览器驱动一条检查 | ✅ 独立（依赖已有 `loadIntoEditor`） |
| **段落行（`kind: "section"`）** | 1–2 天 | `routines.py` 的 `Step`/`validate`/`as_dict`；`service.py` 的 runner 跳过；`console.js` 的 `renderEditor`/`renderTimeline`；CSS | ✅ 独立，且不改变任何现有编排的行为 |
| **纵向运行单** | 2–3 天 | `console.css` 的 `.timeline`/`.step-card` 从行改列；`renderEditor`/`renderTimeline` 重排为 grid；`index.html` 的编辑器结构 | ✅ 独立（纯渲染） |
| **`auto` 等待（`BLOCKING_TOOLS` + 可空 `seconds`）** | 3–5 天 | 上面几项 + `routines.py` 的表与缺省逻辑 + `service.py` 的 runner 与 `preview_routine` 的总时长算法 + 页脚的诚实措辞 + 多条测试 | ⚠️ 需要一次**语义决定**，但可以只加不改（缺省仍按 3.0）|
| **页脚静态检查** | 2–3 天 | 服务端算，页面画。和 `fleet_actions` 一样是"数据而不是判断" | ✅ 独立 |
| **on_failure: "stop"** | 1 天 | `Routine` 加字段、runner 加一个 break、一条测试 | ✅ 独立 |
| **per-dog 目标** | **2–4 周** | `Step.targets`；`validate`；runner 拆步；policy 的语义要在"每狗策略"和"整步策略"之间重新定义；UI 的选择控件；**外加**一套新的不对称告警 | ❌ 不该和上面任何一项捆在一起 |

**周末能做完的**：前面四项（两个 bug + 一个缺口 + 段落行）。
**一周半到两周**：一个完整、可用的运行单（纵向 + 段落 + `auto` + 页脚）。
**一个月**：per-dog。**一个月以上**：任何形式的分支或时间线控件。

### 6.2 风险：更强大的编辑器会不会让操作员编出在硬件上失败的东西？

**会。而且这不是猜测，是 BD 写在自己的文档里的**（§3.1）：

> A consequence of this freedom is that the robot cannot perform some combinations of moves, parameters, and other settings under all conditions.

BD 的应对是 **Beginner/Advanced 分档**，默认 Beginner、参数范围更小、部分动作直接不在列表里。**这是本建议可以照抄的最便宜的一条**：既然舰队动作栏已经把 13 个危险动作放在默认折叠的分组里（`controls.py:208-212` 的注释解释了理由），编辑器也应该有同样的克制——**调色板里的危险动作放在默认折叠的分组里，而从调色板加入的 `robot_play_action` 步骤在运行单里带 ⚠ 标记**（今天 `renderTimeline` 会显示 policy 徽章和每狗的点，但**不显示这一步是否需要净空**）。

但**具体到本建议，风险是低的**，理由是结构性的而不是乐观的：

1. **形状没有变。** 运行单能表达的东西和今天的编辑器完全一样（一串发给所有狗的 fire-and-forget 步骤），只是**读得出来**。**一个不能表达更多东西的编辑器，不能表达更多错误。**
2. **安全层不在编辑器里。** 每一步走 `CONSOLE_TOOLS` 白名单、`confirm`、危险动作闸门、safe mode、限速、审计，全部照旧（`routines.py:20-22` 是一份承诺，不是描述）。**编辑器能让操作员做的，只有"排得更容易"。**
3. **真正增加风险的是 `auto` 等待**——而方向是**减少**风险。今天那些多余的等待让程序更长，而一个 34 秒的程序和一个 20 秒的程序相比，**操作员盯盘的时间更长，但他也更容易失去"现在到第几步了"的注意力**。更重要的是：`auto` 减少了操作员为了让时间"看起来对"而**手填长等待**的动机，而手填的长等待正是"两条狗在半路上被下一个命令打断"这一类失效的来源（`routines.py:314-317` 已经论证过：等待太短会让下一步在狗还在走的时候发出，导致的失效是"转身在迈步中途被打断"）。

### 6.3 最可能出错的地方

- **`auto` 的语义决定。** 如果我把 `seconds` 改成可空，而某条已保存的自定义编排恰好缺这个键，`validate` 不能因为缺省值变了就给它换一个行为。**已保存的编排必须逐字保持它今天的含义**——这条有先例：`routines.py:355-360` 记着"默认值必须复现按钮上今天的东西，而不仅仅是某个看起来合理的东西"。
- **总时长的措辞。** 页脚写上"约 14 秒"而实际是 22 秒，就是把 §1.3 那个错误从步骤级搬到编排级。**要么显示区间，要么显示"≥"，要么把阻塞类的耗时估出来并说明是估的。**
- **纵向列表的可读性会下降。** 卡片条的一个真实优点（虽然代价很大）是"一眼看到全局形状"。40 行的列表需要别的东西来提供形状感——这正是**段落**的作用，也是为什么这两项应该一起做。

---

## 7. 实施者要读什么

按阅读顺序：

**模型与运行器**
1. `unitree_fleet_web/routines.py` 全文（475 行，值得逐行读）——特别是 `:106-139` 的 `Step`、`:142-222` 的 `Routine.as_dict`、`:225-296` 的 `validate`、`:299-328` 的 `_walk_seconds`（那段注释解释了为什么这里是上限）、`:339-408` 的两个 `build`。
2. `unitree_fleet_web/service.py:949-1037` 的 `_run_routine`——**注意 `:977-993` 里 `satisfied` 只改 `record["state"]`，不影响控制流**。
3. `unitree_fleet_web/service.py:1041-1105` 的 `fleet_action`——`asyncio.gather` 在 `:1092`，per-dog 的闭包在 `:1077-1090`。
4. `unitree_fleet_web/service.py:652-684` 的 `preview_routine`——**总时长是在这里算的，改 `seconds` 的语义必须同时改这里**。
5. `unitree_fleet_web/service.py:1359+` 的 `_correct_turn`——唯一一处编排会按狗做不同事情的地方，per-dog 目标落地时要读它。

**阻塞语义（本报告最重要的发现所依赖的证据）**
6. `unitree_robot_agent/tools/locomotion.py:318-380`（`robot_turn`）与 `:383-433`（`robot_walk`）——**两个都以 `await` 结束，都闭环**。
7. `unitree_robot_agent/motion/controller.py:264-360`（`turn` 的比例环）与 `:367-470`（`forward_m` 的到达判定与无里程计回退）。
8. `unitree_robot_agent/tools/locomotion.py:601-631` 的 `_go2_move`——`await asyncio.sleep(duration)` 就是"`robot_move` 阻塞"的全部实现。
9. `unitree_robot_agent/tools/actions.py:35-91` 的 `robot_play_action`——**对照阅读**：它发完 api_id 就返回，这才是文档里说的那种"不报告完成"。两相对照就是 `BLOCKING_TOOLS` 这张表的依据。

**页面**
10. `unitree_fleet_web/static/console.js:1691-1999`——编辑器全段。重点：`:1717-1761` `loadPalette`（看它漏了什么）、`:1763-1863` `renderEditor`（数一数只有两个输入控件）、`:1929-1948` `editorDocument`（看它丢掉了 `dangerous`）、`:1975-1999` `loadIntoEditor`。
11. `unitree_fleet_web/static/console.js:2770-3122`——编排栏与运行显示。重点：`:2809-2843` `routineButton`（内置编排只有运行和参数，没有"编辑"）、`:3059-3122` `renderTimeline`（per-dog lane 在 `:3087-3103`，这是"谁"列的原型）。
12. `unitree_fleet_web/static/console.css:818-1005`——时间线与编辑器的全部样式。`.timeline` 在 `:858-866`，`min-width: 96px` 在 `:872`。
13. `unitree_fleet_web/static/index.html:167-207`——编辑器与运行显示的骨架。

**词汇量**
14. `unitree_fleet_web/controls.py:189-236`（安全/危险/需要净空三张表）与 `:458-556`（`_COMMON_CONTROLS`，看 `robot_walk` 不在其中）。
15. `unitree_robot_agent/robots/go2.py:70-93`——22 个动作的全表，`dangerous` 是第三列。

**测试的写法（必须照做）**
16. `tests/test_console_routines.py`——模型层断言的样子，特别是 policy 的四条。
17. `tests/test_console_routine_params.py`——参数化路径。
18. `tests/test_console_interaction.py:1-60`（为什么没有 Playwright，以及驱动是怎么注入的）与 `:559-640`（在真实页面里点编辑器的那些检查）。**任何新的编辑器交互都要在这里加一条检查，而不是在服务端加断言。**
19. `tests/test_console_payload_contract.py:1-30`——读它的理由：**`dangerous` 丢失正是这个文件存在的目的那一类 bug**，值得先看它为什么没抓到。

**背景与约束**
20. `docs/OPEN_QUESTIONS.md` A1（里程计是否发布）——**这一条直接决定了页脚能不能做碰撞检查**。
21. `docs/DUAL_DOG_CONSOLE_PLAN.md` P1.19（编排的四个加强理由）、P3.2（为什么整条时间线要先可见）、P20.3（为什么中途插入要等步骤边界）。
22. `unitree_fleet_web/sync.py` 的模块文档——**读它是为了知道"我们测不了两只狗在哪"这件事有多大**，以及为什么纠正只能是"一次、有界"。

---

## 附：本报告的不确定之处

- **Go2 动作的观感同质/异质**，来自读 `robots/go2.py` 的名字与描述，**我没有在真机上看过任何一个**。这直接影响 §5 的结论（见 §5 末尾）。
- **PolyScope / UR 示教盒的具体交互**（§3.6）、**FlexBE 的 UI**（§3.3）、**Blockly 的无障碍现状**（§2）——**均未取回原文，标注为 UNVERIFIED**，不应作为决策依据。
- **IEC 61131-3 / PLCopen 的 SFC 语法细节**（动作限定符表、时间型迁移的确切写法）来自搜索片段与二手文献，**PDF 原文取回失败**。结论方向（时间可以是迁移条件）有多份独立来源互相印证，但**具体语法请勿引用本报告**。
- **NTSB / ATSB 的事故报告**未取回正文（403 与仅有新闻标题），因此 §3.2 末尾只作为线索列出。
- **"40 步上限是否真的会被用满"**：没有任何数据。`routines.py:248` 的数字看起来是一个防御性上限而不是经验值。如果操作员从没写过超过 10 步的编排，那么 §4.2(a) 的紧迫性会下降——但我认为纵向布局仍然更好，因为**列宽**比行数更早成为问题（今天一行的"等多久"输入框是 46 px，`console.css:965`）。
