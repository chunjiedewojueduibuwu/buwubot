# 参考来源与致谢

本文区分设计启发、源码研究、运行依赖与素材。固定提交来自原研究记录，表示阅读版本，不表示直接移植；当前分发包不含上游仓库、研究缓存或展示截图。不用一句致谢代替复制/改编代码的版权和许可证。

## 已有阅读记录

| 来源 / 作者或组织 | 阅读版本 | 借鉴点与使用方式 |
|---|---|---|
| [QQ-agent](https://github.com/K0nd1us/QQ-agent) / K0nd1us（许可署名Kondius） | fdfd325e148d91845a0aeea3dfdff4cf92fc61a1 | 设计参考：沉默、真实发送动作、触发/历史分区、人物相关记忆与可回溯整理 |
| [DaFeiYu QQ Bot](https://github.com/nanbengxian-cyber/dafeiyu-qq-bot) / nanbengxian-cyber | 8009d1036e5a0a6fa2a2490429375ff4735c5b67 | 源码研究与部分设计吸收：关系/情绪/任务分区、候选排序和注入预算；并非整套自然度方案移植 |
| [蓝色大肥鱼](https://github.com/RelyLinYu/Xiaojingyu-QQbot) / RelyLinYu | 76f459511e556b82a7f1022fcefaa10d0350a5ab | 设计参考：同会话发送队列、真实回执、稳定人格/上下文/工具事实分区、换措辞保持事实 |
| [Herein](https://github.com/automaton59/Herein) / automaton59 | a8d6560e22f7803c56accbffd7fdc56a9437cf2c | 设计参考：人格模块、受众/场景组织、后台窄任务与状态摘要；未取得作者私人完整人设 |
| [BaiShou-Next](https://github.com/foxletters-hq/BaiShou-Next) / foxletters-hq | cb3bd91d07830b74d3563c73085143df47510bfb | 设计吸收与本项目适配：SQLite实体/别名锚点、有界多跳、时间线、证据路径、修订/删除传播；没有整库迁入 |
| [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) / c1overzz | 1bfcf294efac5c842c485d0146f1235e91ee2efc | 设计参考：发现/深读分离、实际阅读范围、字幕与可选ASR、临时清理和最小权限 |
| [dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale) / Alterssss | eee03c637190aebcbaff5ee8906a6baa49ca2d76 | 设计参考：持久学习安排、续看检查点、字幕/画面互补、抽样拼图；不接入评论/弹幕/私信 |
| [dsh-fish-tone](https://github.com/MX3a48c5/dsh-fish-tone) / MX3a48c5 | b22b071644379ae8abb2640c412eef9ea2de49ee | 表达规则研究：文体与事实保护分开、具体语用与空泛升华检查；不复制统计阈值/随机口癖 |
| [fishtalk](https://github.com/Nozzi2455/fishtalk) / Nozzi2455 | bb621c3da31052e7e52b6307db3e27c508e4d502 | 设计参考：场景分组、具体示范/反例、自我重复检查；未采集其评论语料 |
| [dsh-fish](https://github.com/ssamurias/dsh-fish) / ssamurias | 33cc1e21ae01fd54c7532d5b32ad105892567b75 | 源码研究：经历与倾向分开、有来源偏好、保留修正来由和预算；不声称全部成长机制移植 |
| [AgentSoul](https://github.com/yuhui-sama/dsh-agentsoul) / yuhui-sama | README层 | 内容分区与稳定人格/当前状态分开；模板源码未完整核实 |
| [Silvie](https://github.com/teign07/Silvie) / teign07 | README层 | 固定constitution与可变persona分离、经历整合后检查核心一致性 |
| [dsh-persona-plugin](https://github.com/gulagala001/dsh-persona-plugin) / gulagala001 | 7627d0427a7a785d2c6ea83378202c0c0aa6a534 | 格式思路：身份、风格、沟通、表现分区，人格文本与工具权限分开 |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) / deepseek-ai | persona材料22ddee3792402c3e70ec5f86a63c3e3e895a2f60 | 设计参考：稳定人格前缀与动态运行上下文分离，本项目不是基于其运行框架 |

## 展示图及推荐材料

- “小凌工程”：此前提供的抖音展示图启发意义、细节、可检索性、情绪残留、巩固、干扰的分离，以及线索唤醒/回忆重建。已有记录未找到可确认归属的公开源码；作者公开账号、原作品链接及时间点仍待补充。不声称已审核源码，也不将本项目调参系数归给作者。包内不含截图、角色形象或原笔记图片。
- [dsh-bili-humanism](https://github.com/InariNGT-Official/dsh-bili-humanism)：此前推荐截图中出现的仓库地址，尚无足够阅读/吸收记录；本次LICENSE读取返回404，不能推断没有许可证或仓库一定不存在。仅列推荐来源，不声称已移植。
- [Dify](https://github.com/langgenius/dify)：作为知识库分区/检索工作流的研究对象，未引入整套服务或其源码。

## 许可和追溯范围

本地参考副本可核实QQ-agent、DaFeiYu、scroll、whale存在MIT声明；scroll内的bilibili-mcp另有上游署名。此次通过公开仓库接口核对Herein与dsh-fish当前LICENSE为MIT，BaiShou-Next当前LICENSE为AGPL-3.0-or-later。实际分发应核对所用版本及具体文件，不能把当前默认分支许可自动当成所有历史文件授权。

函数、schema与提示词对照确认白守两组完整分类/谓词的选择和排列及四模式组织进入本项目；fishtalk的部分反例词条集合也有具体交集。这些来源的范围与实现差异见[源码归属核对](docs/SOURCE_ATTRIBUTION.md)。项目采用AGPL-3.0-or-later，保留[NOTICE](NOTICE)及licenses目录中的白守固定版许可/版权和fishtalk完整MIT许可。没有把短语或通用BFS相同直接判定为整个文件复制，也不以无长段精确匹配推定独立创作。

运行依赖单列于[THIRD_PARTY.md](THIRD_PARTY.md)，不与设计参考混为一谈。私人写的人设、记忆、评估输出和研究笔记均不在此包。
