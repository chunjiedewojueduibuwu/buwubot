# 维护者发布指南

本文供维护者在准备上传 GitHub 或公开源码时使用。使用者安装、更新和回退见 [INSTALL_UPDATE.md](INSTALL_UPDATE.md)。上传仓库可以直接使用源码文件，无需另做 Release 附件。

## 发布前核对

- **整体许可**：本项目采用AGPL-3.0-or-later。保持 [LICENSE](../LICENSE)、[NOTICE](../NOTICE)、licenses目录及项目元数据一致；分发改版和运营改版服务时履行对应源码提供与通知要求，见 [许可说明](../LICENSE_OPTIONS.md)。
- **第三方来源**：按实际使用的文件与版本复核 [REFERENCES.md](../REFERENCES.md) 和 [THIRD_PARTY.md](../THIRD_PARTY.md)。确认的复制或实质改编需要保留相应版权、完整许可和必要 NOTICE；设计启发与运行依赖分别说明。
- **文件与历史**：检查当前跟踪文件以及全部准备上传的提交，确认不包含密钥、私人角色、QQ 号、聊天、记忆、媒体、截图、旧研究缓存、环境或模型权重。`.gitignore` 只能防止新文件误入，不会清除已有提交中的内容。
- **提交身份**：确认提交者姓名与邮箱符合公开意愿，必要时使用 GitHub 提供的隐私邮箱。身份以维护者实际选择为准。
- **可使用性**：核对默认首次启动数据为空、本体与 QQ 默认关闭，安装命令与版本保持一致；区分合成测试、隔离启动验证和真实联网接入的结果，不把其中一项当作所有环境的端到端验收。

可在源码根目录查看：

```powershell
git status --short
git ls-files
git log --all --format=fuller
```

另检查被忽略但可能在手工上传时误选的目录：`data`、`.venv`、`.bootstrap`、`.tools`、`runtime`、`dist`、缓存与日志。备份留在私密位置。若任何密钥曾进入准备公开的历史，应先撤销或轮换密钥并处理历史，再发布。

## 使用 GitHub Desktop

1. 用 **File → Add local repository** 选择通用源码仓库。
2. 查看 **Changes** 和 **History**，核对文件、所有提交及提交身份。
3. 选择 **Publish repository**，填写维护者自己的仓库名与描述；可先选择 **Keep this code private** 进行远端复核。
4. 在远端核对文件、历史、许可与来源说明。准备公开时，再由维护者调整可见性。

如果使用源码 ZIP、目录中没有 `.git`，可先用 Desktop 的 **Create a New Repository** 创建仓库，再把审核过的源码放入该仓库。不要把已有私密安装的 `.git` 复制进去。

## 使用命令行

先在 GitHub 创建空仓库；已有本地提交时，避免在网页初始化 README、许可证或 `.gitignore`，以免产生两套独立历史。将下面地址中的账号和仓库名替换为实际值：

```powershell
git remote -v
git branch --show-current
```

确认本地分支为 `main`、尚无 `origin` 时：

```powershell
git remote add origin https://github.com/YOUR-ACCOUNT/YOUR-REPOSITORY.git
git push -u origin main
```

如果已存在 `origin`，先确认它指向目标仓库；实际分支不是 `main` 时，推送实际分支。需要修改远端或处理两套历史时，先核对差异，避免用强推覆盖现有提交。

## 使用 GitHub 网页上传

在 GitHub 新建仓库后，上传核对过的通用源码与说明。按本地跟踪清单挑选文件，避免直接拖入含私密数据的整个工作目录。网页上传不上传 `.git`，会建立远端提交；后续在本机继续维护时，优先克隆该仓库并迁入审核后的源码改动。

## 上传后维护

确认 README、安装指南、来源与许可文件在网页中可读，相对链接有效；仓库地址确定后可再补充准确的克隆示例和问题反馈入口。仓库名无需提前写死在通用安装指南中。

源码版本号与 `pyproject.toml` 保持一致。技能包由 `scripts/package_skills.py` 构建，安装过程见 README；GitHub Release 附件可在需要时单独规划。涉及用户数据格式、插件或记忆策略的更新，说明迁移和回退要求，并保持配套锁文件。
