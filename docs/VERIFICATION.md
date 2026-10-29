# 验证与故障诊断

## 验证环境与边界

验证环境为Windows、Python3.12及锁定依赖。自动测试使用隔离数据库、合成消息和模拟传输，不连接真实QQ群，不使用私人聊天作为分发样本。

2026-10-29 的完整回归为 1221 项通过（190.09 秒）；Ruff、JavaScript 语法、Markdown 相对链接以及桌面/手机界面检查通过。退出诊断的隔离测试包含异常堆栈、原生故障输出通道和启动器退出码。

已有验证包括空白首次启动、配置保存重载、旧配置保留、管理接口、本机边界、持久任务、记忆曲线/检索/清理、技能协议，以及桌面/手机管理页。项目wheel曾在独立目标安装并验证启动；这一项使用已具备依赖的解释器，不等于重新联网下载安装全部依赖、登录QQ或完成全部模型调用。

运行检查：

```powershell
.\Install.cmd -Development
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
node --check src/buwubot/web/app.js
```

Node.js仅用于最后一项开发检查，不是本体运行依赖。首次实际部署仍需按README核对外部服务、账号、模型能力及权限。

## 退出诊断

服务日志位于数据目录的logs子目录：

- `service.log`：启动、管理停止请求及退出原因；Python异常保留traceback。
- `launcher.jsonl`：启动器时间（含时区）、PID、子进程退出码、停止标志及已有实例等分支。
- `fault.log`：入口启用faulthandler时的原生故障输出。如果Python原先已有faulthandler，程序保留其原输出目标，service.log记录existing_handler。

这些记录区分主动停止、普通返回和可捕获异常，不会保证捕获操作系统强制结束、断电或整个启动器被终止。看到日志突然中止时，应同时检查计划任务状态/历史、Windows Application Error/WER和睡眠/关机事件；不能只凭QQ离线认定本体崩溃。

旧stop.request表示管理停止意愿，明确执行Start/Run会按已有语义清除它。非零子进程退出仍按启动器既有规则重试；诊断日志不会替用户改变启停设置。

## 提交问题

提供版本、时间及其时区、Status输出和必要日志片段，先去除凭证、真实账号、私人聊天和资源URL。模型或QQ失联与本体进程退出分别描述。不要直接上传整个data目录或包含私人数据的数据库备份。
