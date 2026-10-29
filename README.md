# BuwuBot

BuwuBot 是在本机运行的 QQ AI 聊天与分层记忆框架。你可以为它配置角色，通过外接 QQ 接入程序收发授权会话，再用模型服务完成对话、图片理解和记忆整理。

首次启动使用通用“AI助手”占位名称，人设、账号、密钥和记忆为空；本体与 QQ 聊天默认关闭。项目版本为 `0.1.0`，采用 [AGPL-3.0-or-later](LICENSE)，版权及上游通知见 [NOTICE](NOTICE)。

## 能做什么

- **QQ 聊天**：授权群聊与私聊、引用回复、合并转发、多图分批理解、按作息暂停，以及可选的主动私聊主号。
- **角色与记忆**：固定人设、可成长核心、滚动上下文、近期与长期记忆、情绪感受；SQLite 保存事实和关系图，可选向量检索。
- **持续任务**：后台整理、来源和版本核对、任务中断恢复；实际发送与正式记忆提交由统一协调器执行。
- **可选技能**：公开搜索与网页读取、B 站视频分段声画读取、可单独开启的闲时学习。
- **本机管理页**：维护角色、模型、会话授权、记忆、技能、表情和作息。

角色成长通过外部记忆影响行为，不训练或修改模型权重。模型会出错，视频转写和采样也有覆盖范围；技能按实际读取的材料返回结果。插件作为可信本机代码运行，进程隔离用于故障处理，不构成安全沙箱。

## 运行前准备

推荐使用 Windows 10/11、Windows PowerShell 和 **Python 3.12**。项目要求 `>=3.12,<3.13`，其他 Python 版本不能作为本体运行环境。源码不包含 Python、虚拟环境、QQ 客户端、模型服务或模型权重。首次安装需要联网下载锁定依赖。

准备以下外部组件：

| 组件 | 用途与要求 |
|---|---|
| Python | 从 [Python 官网](https://www.python.org/downloads/windows/)安装 Python 3.12，保留 Python 启动器；用 `py -3.12 --version` 确认 |
| 模型服务 | 提供兼容 OpenAI Chat Completions 的接口（含 JSON 输出与流式响应）、API 密钥和准确模型名；主意识模型必须是 DeepSeek，辅助模型用于整理、检查和视觉任务，图片/视频画面需要辅助模型支持图片输入 |
| QQ 与接入程序 | 自行安装并登录机器人账号，配置 SnowLuma 或 NapCat 的 OneBot 11 HTTP API 与正向 WebSocket 服务，消息上报格式使用数组 |

本体通过 HTTP 调用 QQ 动作，并主动连接接入程序的 WebSocket 接收事件。QQ 客户端、SnowLuma、NapCat、Agent2API 等分别安装和配置；模型服务可用其他兼容网关。账号注册、QQ 扫码及安全验证、服务费用和使用条款由使用者处理。

## 首次安装与配置

以下命令均在源码根目录执行，即包含 `Install.cmd`、`pyproject.toml` 的目录。下载源码 ZIP 后先完整解压；从 Git 获取源码也可使用同样步骤。

### 1. 安装本体

双击 `Install.cmd`，或在 PowerShell 执行：

```powershell
.\Install.cmd
```

安装器在 `.bootstrap` 中准备固定版本 uv，再按 `uv.lock` 创建项目的 `.venv`，不会把依赖安装进共享 Python 环境。网络失败时保留报错并重试；详细选项见[安装、更新与回退](docs/INSTALL_UPDATE.md)。

### 2. 启动管理页

```powershell
.\Start.cmd
```

浏览器打开 [本机管理页](http://127.0.0.1:8765/)。启动管理服务后，本体和 QQ 仍保持关闭。

默认数据保存在源码目录的 `data`。需要长期使用时，建议从第一次启动就指定独立私密目录，后续命令一直使用同一路径：

```powershell
.\Start.cmd -DataDir "C:\BotPrivate\data"
```

双击启动入口使用默认 `data`，不会自动沿用上次手工指定的目录。更换端口可加 `-Port 8766`，然后打开对应地址。

### 3. 配置角色与模型

在“人格与设置”填写角色名称、人设、机器人称呼及模型连接：

- **模型 API 地址**：填服务提供的 API 基地址，程序会在末尾追加 `/chat/completions`。例如兼容服务常用 `https://服务域名/v1`；不要把完整的 `/chat/completions` 再填进去。
- **API 密钥**：填自己的密钥；无需认证的本机网关可填 `自定义非敏感占位值`。保存时留空表示保留已存密钥。
- **主意识模型**：填服务实际提供的 DeepSeek 模型名，名称必须包含 `deepseek`。默认值是占位配置，不保证你的服务提供该模型。
- **后台辅助模型 / 独立复核模型**：填同一网关实际支持的模型名。辅助模型处理记忆和图片等任务；主意识服务故障时任务等待恢复，不由其他模型代答。

核对作息，默认睡觉时间为 `22:30`，重启后的起床时间为 `06:30`，持续运行时为 `07:30`。语义检索、主动私聊和自主学习可在基本聊天可用后再配置。

### 4. 接入 QQ 并授权会话

在“QQ 聊天”中选择实际接入程序，填写已登录的机器人 QQ 号、OneBot HTTP 地址、WebSocket 地址及对应令牌。默认 `http://127.0.0.1:3000` 和 `ws://127.0.0.1:3001` 只是初始值，需与接入程序一致；WebSocket 令牌未单独设置时使用 HTTP 令牌。

添加允许收发的群或私聊；填写主号 QQ 后，也需要启用主号私聊授权才会允许该会话。只授权你准备让机器人参与的会话。在核对身份、模型、授权和作息后，启用 QQ 聊天及“人格与设置”中的“允许运行”，再从已授权会话呼叫角色，检查管理页状态和任务记录。

SnowLuma 的本机自动恢复默认关闭。需要时可填写已完成首次设置的安装目录和控制台密码；它负责恢复已有连接，QQ 登录和安全验证仍由你完成。

## 日常启停与自启动

```powershell
.\Status.cmd
.\Stop.cmd
.\Start.cmd
```

自定义数据目录时，每次加同一个 `-DataDir`。关闭浏览器不会停止服务；管理页的本体暂停开关停止新的本体工作，`Stop.cmd` 关闭整个管理服务。保存过的设置会在下次启动继续使用，已有安装不会每次重置为停用状态。

Windows 当前用户登录后自启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Manage-BuwuBot.ps1 -Action EnableAutostart -DataDir "C:\BotPrivate\data"
```

取消自启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Manage-BuwuBot.ps1 -Action DisableAutostart -DataDir "C:\BotPrivate\data"
```

任务按数据目录生成名称。移动源码目录后，用同一个数据目录重新注册并核对任务路径。取消自启动不会停止当前服务；上述设置也不会配置 Windows 自动登录或替你启动外部 QQ/模型服务。

## 安装可选技能

本体安装完成后，在源码根目录构建技能 ZIP：

```powershell
.\.venv\Scripts\python.exe scripts\package_skills.py
```

脚本生成 `dist/web_search-1.0.1.zip` 和 `dist/bilibili_video-1.2.2.zip`。在管理页“技能插件”上传对应 ZIP、核对设置后启用；安装后的技能默认停用，可在同页停用或卸载。

- [搜索与网页读取](skill_packages/web_search/README.md)：使用公开搜索和静态网页正文，不需要搜索密钥；登录页、验证码、动态内容和 PDF 有读取限制。
- [B 站视频读取](skill_packages/bilibili_video/README.md)：需自行安装 FFmpeg 并让启动服务的环境能找到 `ffmpeg` 和 `ffprobe`；画面以 1 fps 时序拼图交给辅助模型。音轨转写还需以下独立准备步骤：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Prepare-VideoAudio.ps1
```

脚本下载固定版本的 tiny 语音模型，输出语音 Python 与模型目录；将两者填入视频插件设置。视频插件的声音选项默认开启，未准备语音环境时应先关闭该选项，再进行画面读取。插件读取公开视频，不读取评论、弹幕和私信，也不绕过登录或付费限制。启用视频插件不会自动开启自主学习。

向量检索默认关闭，SQLite 关系图不需要独立数据库服务。本地 embedding 需先安装可选依赖，再在管理页选择本地模型并按需允许首次下载，见[详细安装指南](docs/INSTALL_UPDATE.md)。

## 记忆、数据与更新

记忆设置支持原单指数和“双阶段（快降与长尾）”。**新配置仍默认使用单指数**，可按层切换双阶段。普通记忆按曲线与遗忘阈值处理，已取消固定闲置天数 `max_idle_days` 的硬截断；删除正文默认保留 7 天后清理。公式、回想增强及清理范围见[记忆曲线说明](docs/MEMORY_CURVES.md)。

`data` 包含角色、密钥、QQ 标识、聊天、记忆、媒体、技能及索引，是私密数据目录。密钥保存在本机设置中，不能把它当作加密保险箱；配置页面隐藏密钥不等于磁盘加密。模型服务会收到完成任务所需的对话、记忆和媒体内容，请选择可信服务。管理页仅绑定本机回环地址，保持本机使用。

更新前先在管理页暂停本体、停用 QQ，停止服务后备份完整数据目录及配套源码版本，再更新源码和锁定依赖。回退应恢复匹配的源码、数据、插件和媒体，不能只退代码。具体操作见[安装、更新与回退](docs/INSTALL_UPDATE.md)。

## 排查问题与开发检查

| 现象 | 先检查 |
|---|---|
| 安装失败 | `py -3.12 --version`、网络/代理及安装器的完整错误；不要删除私密数据来重装依赖 |
| 管理页打不开 | `Status.cmd` 使用的数据目录、端口占用和该目录下的 `logs/service.log` |
| QQ 在线但不回 | 本体和 QQ 开关、当前作息、机器人身份、会话授权、OneBot 令牌和管理页任务记录；群聊非定向话题默认观察 15 秒，也可保持沉默 |
| 模型认证或名称错误 | API 基地址、密钥、网关支持的准确模型名、辅助模型的图片输入能力 |
| 视频失败 | FFmpeg 是否在服务环境的 PATH、语音环境/模型路径、视频公开可访问性及实际读取范围 |
| 更新后设置像丢失 | 启动命令是否指向原来的绝对数据目录，双击入口是否误用了默认 `data` |

反馈问题时提供项目版本、复现步骤、状态和必要日志，先去除密钥、QQ 号、对话与媒体 URL 等私密内容。

开发依赖与检查命令：

```powershell
.\Install.cmd -Development
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
node --check src/buwubot/web/app.js
```

最后一项需要另装 Node.js。合成测试覆盖不等于在每台机器完成联网安装、QQ 登录和模型调用；首次部署需在自己的环境按上述步骤验证。

## 许可与来源

项目采用 [GNU AGPL v3 或更高版本](LICENSE)。允许使用、修改和商业使用；分发修改版或通过网络向用户提供修改版服务时，应履行适用的对应源码提供及通知义务。具体使用说明见 [LICENSE_OPTIONS.md](LICENSE_OPTIONS.md)，上游版权和适配范围见 [NOTICE](NOTICE)。个人聊天、凭证、记忆和媒体保存在使用者自己的数据目录，不属于本仓库分发内容。

对外提供修改版机器人服务时，请在交互用户可访问的服务说明、公告或界面提供对应版本源码的获取入口。本机管理页默认只对部署者可见，不自动代替向群友提供源码。

感谢 QQ-agent、DaFeiYu、蓝色大肥鱼、Herein、BaiShou-Next 与 DSH 生态的公开材料，以及“小凌工程”展示图对记忆维度设计的启发。各来源、阅读版本、具体适配及引用范围见 [REFERENCES.md](REFERENCES.md)与[源码归属核对](docs/SOURCE_ATTRIBUTION.md)，依赖和外部工具见 [THIRD_PARTY.md](THIRD_PARTY.md)。源码不含上游仓库、私人角色、原群聊、展示截图或模型权重；上游通知与许可证随仓库保留。

维护者上传仓库时可参考[发布指南](docs/PUBLISHING.md)。
