# 🧚 Fairy（流萤）· 本地优先语音私人助手 MVP

以「情感陪伴 + 接管电脑操作」为核心的本地语音助手。所有对话、记忆、日志**只存在你电脑本地**（`fairy/data/`），除调用云端 API 之外不向外发送数据。

---

## 一、现在能做什么（MVP 范围）

| 能力      | 状态       | 说明                                                                                            |
| ------- | -------- | --------------------------------------------------------------------------------------------- |
| 记忆系统    | ✅ 可用     | 全部对话写入本地 SQLite，重启不丢；支持中文关键词检索（`python main.py --search 关键词` 或控制台「记忆」页）；回答时自动召回相关往事           |
| 语音唤醒    | ⚠️ 两种模式  | ① 真·语音唤醒「Hi Fairy」（需 Picovoice Key，见第四节）② **空格键说话**（无需任何 Key，开箱即用）                            |
| 双向语音    | ✅ 可用     | 说完自动断句 → 小米 MiMo-ASR 识别 → 大模型回复 → 小米 MiMo-TTS 播报                                              |
| 语音打断    | ✅ 可用     | Fairy 说话时你直接插话，它立刻闭嘴接着听你说（戴耳机效果最好）                                                            |
| 电脑操作    | ✅ 可用（受限） | 查时间、列目录、读文件、开程序/网页 **直接执行**；写文件、跑命令等 **必须先确认**；删除/覆盖/外发是**硬闸口**，必须当面确认且无法跳过，批量操作先出「撤销清单」      |
| 桌宠      | ✅ 可用     | 小萤火虫「流萤」常驻桌面：待机呼吸 / 聆听波纹 / 思考转星 / 说话口型四态动画；可拖拽、贴边隐藏、右键菜单；双击预览四种状态                             |
| 控制台 GUI | ✅ 可用     | 双击 `控制台.bat`：五页标签——文字聊天（危险操作弹窗确认）、历史记忆检索、API Key/模型/音色配置（Key 掩码不回显）、**使用统计**（对话次数/操作频率/分类分布/记忆库信息）、人设编辑器、开机自启、体检与审计日志。关闭窗口自动最小化到系统托盘，右键托盘图标可恢复或退出 |
| 声线 / 性格 | ✅ 已预留    | 人设改 `persona/default.md` 或控制台里直接编辑（保存即生效）；音色改 `tts.voice`，后期换 voicedesign/voiceclone 型号即可克隆声线 |
| 与 Pi 协作 | ✅ 可选 | 平时**完全独立运行、不依赖 Pi**；只有你**明确要求**时才调用本机 `pi` 命令行（对话里输入 `/pi 任务`，或点名"用 Pi 帮我…"），且**每次都会先确认**。长期记忆只存流萤这边，**不与 Pi 共享** |

---

## 一·五、一键脚本（都是 CRLF + GBK 编码，双击即用）

| 脚本           | 作用                                     |
| ------------ | -------------------------------------- |
| `启动助手.bat`   | 先自检（19 项全绿才继续），再进入待命（桌宠随语音模式一起出现）      |
| `控制台.bat`    | 图形控制台：聊天、记忆检索、配置管理、体检与审计日志             |
| `桌宠.bat`     | 只启动桌宠小萤火虫（不进入语音对话）                     |
| `键盘对话模式.bat` | 打字对话，不依赖麦克风，排查用                        |
| `麦克风体检.bat`  | 录 3 秒测音量，直接给出该填多大的 `silence_threshold` |
| `检索记忆.bat`   | 输入关键词搜历史对话                             |

> 如果双击后窗口一闪而过：这些脚本已按 Windows 要求存为 **CRLF 行尾 + GBK 编码**，正常情况下不会闪退。仍不行就手动开 cmd 运行：  
> `cd /d D:\流萤\fairy` 然后 `.venv\Scripts\python.exe main.py`

---

## 二、三步跑起来

### 第 0 步：先确认 Key 能用（很多人卡在这）

双击 **`服务体检.bat`**（或命令行 `python main.py --diag`），会分别测三个服务：

| 报错                                 | 含义                           | 怎么办                         |
| ---------------------------------- | ---------------------------- | --------------------------- |
| `402 Insufficient account balance` | 该服务要钱，账号没额度                  | 去对应平台充值/领免费额度，或换服务商（ASR 见下） |
| `401 invalid API key`              | Key 填错了或已失效                  | 重新复制 Key，注意别带空格、别漏字符        |
| TTS 正常但 ASR 402                    | MiMo 的 TTS 限时免费、ASR 要计费，正常现象 | 见下方「换 ASR 服务商」              |

### 第 1 步：填 API Key（一个 Key 管全部）

现在**三关（语音识别 ASR / 语音合成 TTS / 大脑 LLM）全走小米 MiMo Token Plan，共用一个 Key**。

1. 到 <https://platform.xiaomimimo.com/token-plan> 订阅一个套餐
2. 订阅后进「订阅」页，拿到 **`tp-` 开头的 API Key**
3. 用记事本打开 `config.json`，把这个 Key 填进 `mimo.api_key`：

```json
"mimo": { "api_key": "tp-你的Key" }
```

大脑（`llm`）的 `api_key` 留空即可，会自动复用这个 Key。当前大脑模型是 `mimo-v2.5`。

> ⚠️ 注意：小米的 Token Plan Key 是 **`tp-` 开头**，按量付费是 **`sk-` 开头**，两者地址也不同、不能混用。本项目已默认配好 Token Plan 的专属地址 `token-plan-cn.xiaomimimo.com`，你只填 Key 就行。

#### 想换别的大脑模型？

`llm` 是 OpenAI 兼容接口，任何一家（DeepSeek / 通义 / Kimi / 硅基流动等）填对 `base_url` + `model` + `api_key` 就能换；小米系用 `api-key` 头（已自动处理），其他家用 `Bearer`。

#### 换 ASR 语音识别服务商（不改代码，只改配置）

ASR 已做成可插拔。想换别家，改 `config.json` 里的 `asr` 段：

```json
"asr": {
  "provider": "whisper_api",
  "api_key": "那家的 Key",
  "base_url": "那家的接口地址（以 /v1 结尾）",
  "model": "那家控制台里写的语音识别模型名",
  "language": "zh"
}
```

`provider` 两个取值：

- `mimo` —— 小米 MiMo-V2.5-ASR（默认，走 chat/completions 的 input_audio）
- `whisper_api` —— 标准 OpenAI 的 `/audio/transcriptions` 接口，**凡是兼容这个接口的服务商都能填**（OpenAI、Groq、以及国内各家提供该兼容接口的平台）。模型名请从对方控制台复制，别自己猜。

> 兜底：即使识别服务挂了，程序也会提示你**改用键盘把这句话打出来**，不影响继续对话。

### 第 2 步：双击 `启动助手.bat`

它会先跑一遍自检（19 项，全绿才继续），然后进入待命状态。

### 第 3 步：按【空格】说话

- 按空格 → 听到「叮」一声 → 说话 → 说完停顿约 1 秒自动结束
- 按 `Q` 退出
- 想先不插麦克风试跑：`python main.py --text`（打字对话）

---

## 三、常用命令

```bash
python main.py               # 语音模式（默认）
python main.py --text        # 键盘打字模式，不依赖麦克风
python main.py --selftest    # 离线自检（不联网、不需要 Key）
python main.py --devices     # 查看麦克风/喇叭设备编号（声音不对时用）
python main.py --search 豆豆  # 检索历史对话
python main.py --no-speak    # 只显示文字不播报（省 API 额度）
python main.py --stats       # 查看使用统计（对话次数/操作频率/分类分布）
python main.py --pi-check    # 体检：本机 pi 命令行是否可用
```

### 让 Pi 帮忙（`/pi`，可选）

在**键盘模式**或**控制台对话框**里输入：

```
/pi 帮我看看这个项目的结构
/pi 把 README 里的错别字改一下
```

- 只有你这样**明确要求**时才会调用本机 `pi` 命令行；平时流萤**完全独立运行、不依赖 Pi**
- 每次调用前都会**当面确认**（这属于硬闸口，自动流程绕不过）
- 想让 Pi 只读不写：把 `config.json` 的 `pi.read_only` 设为 `true`（只给 `read/grep/find/ls` 工具）
- **长期记忆只存在流萤这边**，不与 Pi 共享；Pi 的结果会作为"笔记"记进流萤自己的记忆库
- 体检：`python main.py --pi-check`（或看 `config.json` 的 `pi` 段）

#### 对外接口（给外部程序调用）

若你想让别的程序/Pi 扩展反过来调用流萤，用 `fairy_api.py`（stdout 单行 JSON）：

```bash
python fairy_api.py ping                      # 健康检查
python fairy_api.py context --query 关键词     # 取"人设+相关往事"整块
python fairy_api.py remember --role user --text "记住这句话"
python fairy_api.py say --text "用流萤的声音念这句"
python fairy_api.py search --query 豆豆        # 检索历史记忆
```

设备不对时，在 `config.json` 的 `audio.input_device` / `output_device` 填 `--devices` 查到的编号（不填则使用系统默认设备）。

---

## 四、配置真·语音唤醒「Hi Fairy」（可选，3 步）

当前默认是**空格键兜底**，想升级成喊名字：

1. 打开 <https://console.picovoice.ai/> 注册（个人免费），复制 **AccessKey**
2. 在控制台的 Porcupine 页面 **创建唤醒词**，输入 `Hi Fairy`，选 Windows，下载 `.ppn` 文件
3. 把两项填进 `config.json`：

```json
"wake": {
  "porcupine_access_key": "你的 AccessKey",
  "porcupine_keyword_path": "C:/路径/Hi-Fairy_en_windows_v3_0_0.ppn",
  "sensitivity": 0.85
}
```

然后安装唤醒依赖：`pip install pvporcupine`（程序会自动检测，有 Key 就走语音唤醒，没有就继续用空格）。

> 想完全离线、不依赖 Picovoice？进阶路线是 openWakeWord 自训练「Hi Fairy」模型，需要录几十条语音样本 + 训练，后续阶段再做。

---

## 五、数据都存在哪（本地优先）

| 内容      | 位置                                |
| ------- | --------------------------------- |
| 全部对话记忆  | `data/memory.db`（SQLite，可随时备份/删除） |
| 操作审计日志  | `data/audit.log`（每一次电脑操作都留痕）      |
| 人设 / 性格 | `persona/default.md`（直接改文字即可）     |
| 全部配置    | `config.json`                     |

**安全闸口（不可关闭）**：

- 写文件、跑命令、复制/移动文件 → 必须先二次确认（命令行输入 y / 控制台弹窗）
- **硬闸口**：删除文件、覆盖已有文件、外发/付款类操作 → 无论配置如何都**必须当面确认**，自动流程绕不过
- **撤销清单**：批量操作执行前列出全部待执行动作，你一次性确认或取消
- 受保护目录：磁盘根目录、你的用户主目录、项目目录本身，删除类操作一律拒绝
- 所有操作（含被你拒绝的）写入 `data/audit.log`，控制台「状态」页可随时翻查

---

## 六、出问题先看这里

| 现象                    | 排查                                                                                                             |
| --------------------- | -------------------------------------------------------------------------------------------------------------- |
| **双击 bat 窗口一闪就没**     | 脚本已是 CRLF+GBK（2026-09-07 修复）。仍闪退就手动开 cmd：`cd /d D:\流萤\fairy` → `.venv\Scripts\python.exe main.py`              |
| **按空格没反应**            | ① 黑窗口被鼠标点过会进「标记模式」吞按键 → 按 **Esc** 解除；② 当前是空格键兜底模式，**按空格后直接说话，不用喊 Hi Fairy**；③ 跑 `麦克风体检.bat` 看是不是没收到声音          |
| **喊「Hi Fairy」没反应**    | 没配 Picovoice 时**不会**本地识别唤醒词；但已加云端兜底——你说的话被识别成"Hi Fairy"时，助手会回一句"我在呢"并接着听你说。想要真·本地唤醒见第四节                        |
| 说"未配置 API Key"        | `config.json` 里 `mimo.api_key` / `llm.api_key` 没填                                                              |
| 需要装 Pi 吗            | **不装也能用**。流萤平时完全独立运行，只在你说 `/pi 任务`（或点名"用 Pi 帮我"）时才去调本机 `pi` 命令行。没装或没配好时，`/pi` 会明确提示"没找到 pi 命令"，其余功能一切照常 |
| **回复时最后两三个字没念出来**     | 蓝牙耳机/部分声卡的"尾部截断"毛病，已自动补 0.8 秒静音垫底。仍被截就把 `config.json` 里 `audio.output_tail_silence` 调大到 1.0~1.5                |
| 听不到我说话 / 识别全是空白       | `python main.py --devices` 找到你的麦克风编号，填进 `audio.input_device`；安静环境把 `silence_threshold` 调小到 0.008               |
| 话没说完就被截断              | 把 `audio.tail_silence_seconds` 调大到 1.5~2.0                                                                     |
| 没说话它也在听               | 把 `silence_threshold` 调大一点（0.02~0.03）                                                                          |
| 回应很慢                  | 语音链路是云端往返，正常 1-3 秒；`llm.max_history_turns` 调小可加速                                                               |
| TTS / ASR 报 404 或 500 | 检查 `mimo.base_url` 是否为 `https://api.xiaomimimo.com/v1`                                                         |

---

## 七、目录结构

```
fairy/
├─ main.py              入口（对话编排 / 唤醒 / 主循环）
├─ gui.py               图形控制台（tkinter）
├─ fairy_api.py         对外接口：给外部程序/Pi 扩展调用的单行 JSON CLI
├─ config.json          全部配置（Key、麦克风、灵敏度、安全策略、pi 段）
├─ persona/default.md   人设（性格、说话风格）
├─ core/
│  ├─ memory.py         记忆：SQLite 持久化 + 中文检索 + 召回
│  ├─ asr.py            语音识别：MiMo-V2.5-ASR
│  ├─ tts.py            语音合成：MiMo-V2.5-TTS
│  ├─ llm.py            大脑：OpenAI 兼容接口 + 动作指令解析
│  ├─ audio_io.py       录音（自动断句）/ 播放
│  ├─ wake.py           唤醒：Porcupine + 空格键兜底
│  ├─ pet.py            桌宠「小萤火虫流萤」四态动画
│  ├─ actions.py        电脑操作工具集（含 pi_agent）
│  ├─ agent_backend.py  外部 agent 后端注册表（出站扩展点）+ PiCliBackend
│  ├─ config.py         配置加载（支持 FAIRY_ROOT 覆盖）
│  ├─ stats.py          使用统计
│  └─ safety.py         安全闸口 + 审计日志
├─ data/                记忆库与日志（运行时生成）
├─ tests/smoke_test.py  离线自检用例（19 项）
└─ 启动助手.bat         一键启动
```

---

## 八、下一阶段（MVP 之后按序推进）

1. ~~语音打断（barge-in）~~ ✅ 已完成
2. ~~桌宠形象（四态动画）~~ ✅ 已完成（后续可升级 Live2D）
3. ~~独立 GUI 操作台~~ ✅ 已完成（`控制台.bat`）
4. ~~声线切换~~ ✅ 已完成（GUI 配置页支持预置音色 / voicedesign 文字设计 / voiceclone 音频复刻）
5. ~~电脑操作增强~~ ✅ 已完成（新增搜索/批量重命名/文件整理/压缩解压/系统信息）
6. ~~记忆增强~~ ✅ 已完成（分类/重要度/标签/过期/统计）
7. ~~使用统计~~ ✅ 已完成（`python main.py --stats`，GUI 状态页展示）
8. ~~与 Pi 协作~~ ✅ 已完成（按需调用 `pi` CLI：`/pi 任务`，每次确认，记忆不共享）
9. 对外接口硬化（协议版本号 / 注入体量可选），为将来做 Pi 扩展铺路
10. 全权接管的安全放开（在硬闸口框架下逐步开放更多操作，如批量整理文件）
11. openWakeWord 自训练「Hi Fairy」（彻底离线唤醒，摆脱 Picovoice Key）

