# 安装、更新与回退

本文面向部署和维护 BuwuBot 的使用者。首次配置角色、模型和 QQ 的步骤见 [README](../README.md)；上传代码仓库见[维护者发布指南](PUBLISHING.md)。

## 安装环境与锁定依赖

本体要求 Python `>=3.12,<3.13`。在 Windows 安装 Python 3.12 与 Python 启动器后，打开源码根目录的 PowerShell：

```powershell
py -3.12 --version
.\Install.cmd
```

`Install.cmd` 调用 `scripts/Manage-BuwuBot.ps1 -Action Install`，在项目内创建：

| 目录 | 内容 |
|---|---|
| `.bootstrap` | 安装器环境和固定版本 `uv==0.13.4` |
| `.venv` | 按 `uv.lock` 安装的本体运行环境 |
| `.tools` | uv 缓存及按需下载的 Python |

依赖同步使用 `uv sync --locked --python 3.12`。系统需要有可创建安装器虚拟环境的 Python，uv 再为本体选择 Python 3.12；缺失时可能需要联网下载。安装失败后可修复网络或代理再重试，保留报错以便定位。源码 ZIP 不提供离线运行环境。

可选依赖：

```powershell
# 开发检查所需的 pytest、Ruff 等
.\Install.cmd -Development

# 本地 CPU embedding
.\Install.cmd -LocalEmbedding

# 同时保留两组可选依赖
.\Install.cmd -Development -LocalEmbedding
```

后续重新执行安装/更新时，继续传入所需选项；同步会按当次选择调整环境。本地 embedding 还需要在管理页选择本地模式、模型及下载权限；安装依赖本身不会开启向量检索。选择外部 embedding API 时填写自己的接口、模型与密钥。

## 选择数据目录并启动

推荐把长期使用的数据放在源码目录之外。例如 `C:\BotPrivate\data`，这是使用者自行选择的目录，不需要预先放入配置文件：

```powershell
.\Start.cmd -DataDir "C:\BotPrivate\data"
.\Status.cmd -DataDir "C:\BotPrivate\data"
```

打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)，按 README 配置模型、角色和会话授权。没有 `-DataDir` 时，默认使用当前源码目录下的 `data`。所有启停和自启动命令始终使用同一绝对路径；双击入口不会记住之前手工指定的目录。

换用其他端口：

```powershell
.\Start.cmd -DataDir "C:\BotPrivate\data" -Port 8766
```

管理页地址相应变为 `http://127.0.0.1:8766/`。同一数据目录只运行一个实例，不同数据目录使用不同端口。

停止与查看状态：

```powershell
.\Stop.cmd -DataDir "C:\BotPrivate\data"
.\Status.cmd -DataDir "C:\BotPrivate\data"
```

`Status` 报告运行、进程、端口及管理服务可达状态。停止失败时先检查该目录的 `logs\service.log`；确需强制结束时可显式使用 `Stop.cmd -Force -DataDir "C:\BotPrivate\data"`，强制结束可能打断正在处理的任务，应在再次启动后核对记录。

管理页的本体暂停、QQ 停用与整个服务停止是不同操作。已有设置会持久保留，重新启动不自动清除本体或 QQ 的启用状态。

## 自启动与移动代码目录

为 Windows 当前用户登录后启动注册任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Manage-BuwuBot.ps1 -Action EnableAutostart -DataDir "C:\BotPrivate\data" -Port 8765
```

取消任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Manage-BuwuBot.ps1 -Action DisableAutostart -DataDir "C:\BotPrivate\data"
```

任务名 `BuwuBot-<目录哈希>` 根据数据目录计算，操作指向当前源码目录。移动源码目录后，在新位置以相同数据目录重新执行 `EnableAutostart`，并在任务计划程序核对脚本路径、端口和数据路径。若更换数据目录，先用旧路径取消旧任务，避免两个任务分别启动。

这是登录后启动，不设置 Windows 自动登录。取消任务会保留当前运行服务和数据；仍需执行 `Stop` 才会停止服务。外部 QQ 客户端、接入程序和模型网关按各自方式启动；可选 SnowLuma 自动恢复由管理页单独配置。

## 构建和安装技能

```powershell
.\.venv\Scripts\python.exe scripts\package_skills.py
```

当前生成：

- `dist/web_search-1.0.1.zip`
- `dist/bilibili_video-1.2.2.zip`

在“技能插件”上传 ZIP，填写设置后启用。ZIP 是安装插件用的包，不能用整个项目源码 ZIP 代替。安装后默认停用；已安装技能保存在所选数据目录中，更新源码不会自动把它们替换为新的技能版本。

视频画面需要 FFmpeg。自行安装后在启动服务所用的 PowerShell 中运行 `ffmpeg -version` 和 `ffprobe -version` 确认可用；修改 PATH 后需让后续启动的服务继承新环境。音轨语音转写需要额外执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Prepare-VideoAudio.ps1
```

脚本创建 `runtime\video-asr` 独立环境，安装固定版本 faster-whisper，并下载固定修订的 tiny 模型。将输出的 Python 路径和模型目录填到视频插件设置。运行时读取本地权重，不自动下载。声音默认开启；未准备语音环境时先关闭声音选项。细节见[视频技能说明](../skill_packages/bilibili_video/README.md)。

升级插件前备份数据并记录旧插件版本与设置，安装新 ZIP 后核对设置和启用状态。插件能执行本机代码，只安装可信来源。

## 更新与备份

更新使用同一个数据目录，并为源码和数据保留成套回退点。

1. 在管理页暂停本体、停用 QQ，等待正在处理的工作结束，记录版本、源码位置、数据目录及自启动任务。保存后的停用状态也会进入备份。
2. 执行同一数据目录的 `Stop`，通过 `Status` 确认已经停止。
3. 备份**完整数据目录**，同时保存对应源码版本、已用技能 ZIP 和本机可选工具配置。备份放在仓库外的私密位置；SQLite 数据库要与同目录相关文件一起保存。
4. 获取新版本源码。Git 安装先检查 `git status --short`，妥善处理自有修改后再更新；ZIP 安装可解压到新代码目录，继续使用原来的绝对数据目录。不要从旧目录复制 `.git` 或虚拟环境到新版本。
5. 在新源码目录运行 `Install.cmd`，按需附上 `-Development` / `-LocalEmbedding`。如用视频转写且更换代码目录，也应重新准备独立环境并更新插件里的绝对路径。
6. 使用原 `-DataDir` 启动，保持本体和 QQ 停用，核对角色、核心、记忆、凭证设置状态、会话授权、插件和任务记录。启动可能自动执行数据库迁移，保留更新前的完整备份。
7. 核对模型与 QQ 连接后人工恢复聊天。代码目录有变化时，用相同数据目录更新自启动任务路径。

示例备份命令在**完成暂停和停止后**执行。将示例路径替换为自己的路径；备份目标应是新目录：

```powershell
Copy-Item -LiteralPath "C:\BotPrivate\data" -Destination "C:\BotPrivate\backup-before-update" -Recurse
```

不要在服务写数据库时直接复制或覆盖数据库。备份包含密钥、聊天和媒体，按私密数据保管。

记忆更新保留旧曲线选择；新配置同样默认 `exponential`。可在管理页按层选择 `dual` 双阶段，切换时保持当前剩余程度。旧 `max_idle_days` 字段兼容读取后丢弃，不再按固定闲置天数截断；阈值、正文清理和备份副本的含义见[记忆曲线说明](MEMORY_CURVES.md)。

## 回退

暂停本体、停用 QQ 并停止新版本，先保留一份当前数据供排查。恢复更新前的源码和完整数据备份，包括插件、媒体及索引，再用对应锁文件重建依赖。不要让旧代码直接打开已被新版本迁移的数据；不要把新旧数据目录合并覆盖。数据目录中的派生索引也应来自配套版本，或按该版本的工具重建。

用恢复后的数据目录启动，核对管理页、设置和记录，人工恢复聊天。更新自启动任务指向恢复后的代码入口。回退会丢失备份之后的新记录；需要保留时先单独导出并人工核对。

## 维护者：从已有私密安装迁移

迁移代码入口与发布源码是两件事。通用源码仓库只保存程序、测试与说明；已有私密数据继续留在本机，不能把旧项目整体目录或 Git 历史复制到通用仓库。

1. 按“更新与备份”暂停本体、停用 QQ、停止旧服务并保存完整回退点。
2. 新代码先指向**备份副本**，使用不同端口，确认副本中本体和 QQ 已在备份前停用，再检查迁移与旧设置保留情况。
3. 停止副本实例，再让新代码指向正式数据目录；不能让两个实例同时使用同一份数据。
4. 核对角色、记忆、凭证、插件及会话授权；更新原自启动任务，使数据目录与代码入口保持唯一对应，再人工启用。
5. 稳定后归档旧源码和环境，保留迁移前检查点。之后通用功能在一个代码源维护，私密设置通过管理页维护。

高级用法：`Start`、`Run`、`Stop`、`Status` 和自启动可传 `-PythonPath` 指向已具备兼容依赖的解释器。脚本始终从当前源码的 `src` 加载程序；`Install` 仅管理项目自身环境，不能与 `-PythonPath` 混用。
