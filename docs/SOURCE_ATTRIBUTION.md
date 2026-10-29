# 源码归属与适配范围

本项目按AGPL-3.0-or-later分发，具体版权与许可文件见[NOTICE](../NOTICE)和[LICENSE](../LICENSE)。本说明基于固定参考版本、具体源码及实现演化记录，区分材料再使用、来源适配与功能共性，不以致谢或简单字符串查重代替归属核对。

## 保留的来源材料

| 来源与固定版本 | 本项目位置 | 具体范围与处理 |
|---|---|---|
| BaiShou-Next `cb3bd91d07830b74d3563c73085143df47510bfb`，AGPL-3.0-or-later | `src/buwubot/memory_graph.py`、`memory_prompts.py`，及`consolidation.py`/`memory.py`中的对应schema和路由 | 10类实体、6类关系的选择与排列，以及search/neighbors/timeline/network模式组合有明确来源链。保留来源适配，加入固定版完整许可、Foxletters版权、legal/COPYRIGHT及修改说明；项目整体选择兼容的AGPL，而非先用宽松许可覆盖它。 |
| fishtalk `bb621c3da31052e7e52b6307db3e27c508e4d502`，MIT | `src/buwubot/chat_cliches.py` 中反例词条集合 | 盖章式接话、总结腔/对仗类反例有具体短文字交集。保留完整MIT及原版权通知，并在目标文件标注来源。没有采用其评论语料、LCS实现或统计阈值。 |

白守所核对文件包含`packages/database/src/schema/graph.ts`、图遍历、图RAG、实体对齐/抽取提示及身份工具。BuwuBot使用QQ账号/会话/人格范围、正式事实修订、独立审核和本项目SQLite布局；身份哈希、遍历预算和回退顺序与上游不同。没有确认长段算法或提示词逐句移植，但分类及组织来源不能被这些实现差异抹去。因此保留对应通知，而不将所有通用图算法或相同API名字视为版权表达。

固定版本原文：

- [白守LICENSE](../licenses/BaiShou-Next-LICENSE.txt)
- [白守COPYRIGHT](../licenses/BaiShou-Next-COPYRIGHT.txt)
- [fishtalk MIT](../licenses/fishtalk-MIT.txt)

这些源码许可不授予上游商标、官方角色和美术资产的使用权；本项目不包含这些资产。

## 功能和设计参考

- QQ-agent：沉默、触发上下文、真实发送和记忆追溯；本项目使用持久SQLite任务及最终协调器，未采用其完整状态机。
- DaFeiYu：候选分区与预算等做法；未采用其跨插件评分实现。
- 蓝色大肥鱼：队列、回执、稳定与动态材料分区；未采用其Promise队列或复读状态表。参考固定版根LICENSE未得到确认，不假定它是MIT，也不分发其文件。
- Herein、dsh-fish-tone、dsh-fish：研究表达、角色分区和成长；没有将完整人格卡、随机口癖或评论语料作为本项目素材分发。
- dsh-bili-scroll、dsh-bili-whale：研究发现/深读、片段覆盖和断点续看；本项目采用自己的任务、插件协议、画面与音轨实现，不引入评论/弹幕/私信能力。
- dsh-persona-plugin、DeepSeek Harness：人格文本与工具权限、稳定前缀与动态上下文的分离；不是本项目运行框架。
- Dify：知识库与检索工作流研究，没有引入服务或已确认的函数移植。

对应仓库、作者及阅读版本见[REFERENCES.md](../REFERENCES.md)。

## 材料边界

AgentSoul与Silvie只核对到README层；“小凌工程”由展示图提供记忆维度及公式思路，未得到可确认归属的源码；dsh-bili-humanism仅有推荐地址，未确认实际吸收代码。这些条目不是授权证明。本项目没有附带其截图、完整角色模板或模型资产，也没有为未知许可填写一个猜测值。

本核对没有对所有上游文件的每个分支出具法律结论。后续新增上游片段、模板或素材时，需按具体文件及版本补充版权、许可及适配范围。依赖及可选外部程序见[THIRD_PARTY.md](../THIRD_PARTY.md)。
