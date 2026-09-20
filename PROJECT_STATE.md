# 项目交接 · 流萤 Firefly 本地语音助手

## 任务与版本

- **项目路径**：`D:\流萤`
- **交接时间**：2026-09-20
- **交接编号**：HANDOFF-20260920-001
- **当前目标**：增加 Harness（任务编排与执行框架）能力，增强 vibecoding 能力
- **技术栈**：Python 3.13 + Tkinter GUI + MiMo LLM/ASR/TTS
- **分支/提交**：Git 仓库，最新改动未提交
- **EXE 位置**：`D:\流萤\dist\流萤.exe`（33.1 MB，已构建）

## 已完成的关键工作

### 1. 配置页面滚动功能（本次新增）

配置页面内容太多（API Key、LLM、TTS、ASR、桌宠、记忆、情绪、Pi协作、人设编辑器等），底部内容被挤到界面下方看不见。已实现滚动功能。

**修改文件**：`gui.py`

**改动详情**：
- `_build_config_tab` 方法：用 Canvas + Scrollbar 包裹配置内容框架，实现垂直滚动
- 底部「保存配置」「保存人设」按钮固定在 `side="bottom"`，不随内容滚动
- 新增 `_on_tab_changed` 方法：切换标签页时自动管理鼠标滚轮绑定（只在配置页启用）
- `_quit_app` 方法：退出时清理鼠标滚轮绑定

**滚动特性**：
- 鼠标滚轮滚动配置内容
- 内部框架宽度自适应 Canvas 宽度
- 切换到其他标签页时自动禁用滚轮，避免干扰

### 2. Harness 任务框架（前次完成）

完整实现了任务编排与执行框架，支持 Pi 任务异步执行、连续命令、状态查询。

**新增文件**：
| 文件 | 用途 |
|------|------|
| `core/harness.py` | Harness 核心：TaskQueue（任务队列）、TaskState（状态机）、AsyncExecutor（异步执行器） |
| `core/harness_middleware.py` | 中间件：任务状态查询、连续命令解析、上下文整合 |
| `tests/test_harness.py` | Harness 单元测试（24 项，全部通过） |

**修改文件**：
| 文件 | 改动 |
|------|------|
| `core/agent_backend.py` | 新增 `run_with_cancel` 方法，支持取消和实时输出 |
| `main.py` | 注入 harness 实例，改造对话主循环支持异步任务 |
| `gui.py` | 对话页添加任务状态面板（可折叠） |
| `core/pet.py` | 新增 "working" 状态动画 |
| `core/memory.py` | 新增 `add_task_result` 方法 |
| `core/safety.py` | 添加注释说明 harness 异步执行仍受硬闸口约束 |
| `persona/default.md` | 添加任务执行行为规范 |
| `config.json` | 新增 `harness` 配置段 |
| `tests/smoke_test.py` | 更新桌宠状态数检查（4→5） |

**功能特性**：
- Pi 任务提交到后台队列异步执行，不阻塞对话
- 支持连续下达多个命令，任务排队执行
- 可随时查询进度（问"任务做得怎么样了"）
- 支持取消任务
- 控制台对话页有任务状态面板
- 所有安全闸口（硬闸口）完整保留，任务提交前必须确认

### 2. Bug 修复

**Task.duration 属性不能 set 的 bug**：
- **原因**：`gui.py` 和 `main.py` 中创建 Task 对象后直接设置 `task.duration`，但 `duration` 是只读属性
- **修复**：修改 `format_task_list` 支持 dict 输入，不再需要创建 Task 对象
- **影响文件**：`core/harness_middleware.py`、`gui.py`、`main.py`

## 关键文件

| 文件 | 用途 |
|------|------|
| `firefly.py` | 唯一入口（打包与开发共用） |
| `main.py` | Firefly 主类：对话、语音、桌宠、统计、Harness |
| `gui.py` | Tkinter GUI（ConsoleApp + ChatWorker + 任务面板） |
| `core/harness.py` | Harness 任务框架（TaskQueue/AsyncExecutor） |
| `core/harness_middleware.py` | Harness 中间件（任务状态查询/连续命令解析） |
| `core/agent_backend.py` | 外部 Agent 后端（Pi CLI，支持异步执行） |
| `core/safety.py` | 安全闸口（hard/soft confirm + 审计日志） |
| `core/memory.py` | 记忆系统（SQLite，支持任务结果存储） |
| `core/pet.py` | 桌宠（FireflyPet，支持 working 状态） |
| `config.json` | 运行时配置（含 harness 配置段） |
| `tests/smoke_test.py` | 离线自检（42 项，全部通过） |
| `tests/test_harness.py` | Harness 框架单元测试（24 项，全部通过） |

## 有效决定

1. **情绪实例全局唯一**：由 ChatWorker 持有并注入 `Firefly(emotion=...)`；热重载走 `apply_config()`
2. **Pi 调用是硬闸口**：每次调用都必须当面确认，不可被 auto_confirm 跳过
3. **API Key 从不回显**：安全设计，输入新值后覆盖旧值
4. **_poll_ui 异常隔离**：单条消息异常不阻断整个轮询循环，防止 GUI 冻死
5. **Harness 异步任务框架**：Pi 任务可提交到后台队列异步执行，不阻塞对话；所有安全闸口完整保留
6. **任务状态可查询**：用户可随时问"任务做得怎么样了"查看进度，支持取消任务
7. **format_task_list 支持 dict**：避免创建 Task 对象时设置只读属性的错误

## 验证状态

| 项目 | 状态 |
|------|------|
| 配置页面滚动 | ✅ 代码导入成功，语法检查通过 |
| EXE 构建 | ✅ 成功（33.1 MB） |
| Harness 单元测试（24项） | ✅ 全部通过 |
| 离线自检（42项） | ✅ 全部通过 |
| 对话功能 | ✅ 正常 |
| 任务状态查询 | ✅ 正常 |

## 接续动作

1. **下一步**：
   - 实机测试配置页面滚动功能是否正常工作
   - 实机测试连续多轮 Pi 任务（第一轮做游戏，第二轮改游戏），验证任务面板正常显示
2. **前置条件**：无阻塞项
3. **验收方法**：
   - 启动 EXE → 切换到「配置」页 → 鼠标滚轮滚动查看所有配置项 → 底部保存按钮始终可见
   - 输入 `/pi 帮我做个小游戏` → 确认 → 再输入 `/pi 帮我修改这个游戏` → 确认 → 查看任务面板无报错
4. **已知问题**：用户报告第二轮任务未执行，需进一步排查（可能是任务提交流程问题）

## 已确认无效的路线

- ~~config.json 缺少 pi 段是唯一原因~~ → 第二次故障证明 `_poll_ui` 异常处理也是根因
- ~~Task.duration 可直接设置~~ → 是只读属性，需改用 dict 输入
