# 第三方组件与素材

安装版本由uv.lock锁定；下表依据本机已安装分发元数据，不表示将这些代码或环境打包发布。更换版本后应重新核对许可证。传递依赖也保留各自许可证，不能被项目整体许可证覆盖。

| 依赖 | 检查版本 | 许可元数据 |
|---|---|---|
| [fastapi](https://pypi.org/project/fastapi/) | 0.156.4 | MIT |
| [uvicorn](https://pypi.org/project/uvicorn/) | 0.54.0 | BSD-3-Clause |
| [httpx](https://pypi.org/project/httpx/) | 0.28.1 | BSD-3-Clause |
| [websockets](https://pypi.org/project/websockets/) | 16.1.1 | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/) | 2.14.1 | MIT |
| [mem0ai](https://pypi.org/project/mem0ai/) | 1.0.11 | Apache-2.0 |
| [qdrant-client](https://pypi.org/project/qdrant-client/) | 1.19.1 | Apache-2.0 |
| [tzdata](https://pypi.org/project/tzdata/) | 2026.5 | Apache-2.0 |
| [pillow](https://pypi.org/project/pillow/) | 12.3.0 | MIT-CMU |
| [psutil](https://pypi.org/project/psutil/) | 7.2.2 | BSD-3-Clause |
| [sentence-transformers](https://pypi.org/project/sentence-transformers/) | 5.7.0 | Apache-2.0 |
| [pytest](https://pypi.org/project/pytest/) | 9.1.1 | MIT |
| [pytest-asyncio](https://pypi.org/project/pytest-asyncio/) | 1.4.0 | Apache-2.0 |
| [ruff](https://pypi.org/project/ruff/) | 0.17.1 | MIT |

## 外部程序和可选模型

- QQ客户端、SnowLuma/NapCat、Agent2API：外部配置组件，未捆绑程序、账号、登录态或许可文件；按各自条款安装使用。
- FFmpeg：可选外部解码器，不随包提供；不同构建的LGPL/GPL条件需要按实际发行版核对。
- faster-whisper/CTranslate2及tiny语音模型：可选独立安装；模型仓库、权重和代码的许可分别核对，包内没有权重。
- sentence-transformers/BGE：可选下载，库与模型分别核对许可；默认不加载。

## 本包资源

包内网页使用原生HTML/CSS/JavaScript和简单SVG字母图标，没有打包上游人物图片、B站/抖音截图、表情库或字体文件。测试为合成数据，不提供原群聊媒体。参考来源见[REFERENCES.md](REFERENCES.md)。

项目整体采用AGPL-3.0-or-later。实体/关系分类与检索组织的白守来源适配、fishtalk部分反例集合的使用范围见[NOTICE](NOTICE)，固定版本完整许可及版权文本保存在licenses目录。安装依赖保留各自许可，不能被项目整体许可覆盖。具体来源边界见[源码归属核对](docs/SOURCE_ATTRIBUTION.md)。
