# 第三方组件与致谢

本仓项目代码按 [`LICENSE`](LICENSE) 中的 MIT 许可发布；第三方组件保留各自的版权与许可声明。
下面列出主要依赖、借鉴来源及**不随仓发布**的内容。

## 运行时依赖

- **nanonis_spm**—— 通过 Nanonis TCP 协议与控制器通信的 Python 接口。依赖版本要求见
  `MASTv2/requirements-v2.txt`。本仓在 `MASTv2/mast/core/nanonis_patch.py` 中提供运行时补丁，
  处理响应解析、TCP 接收及部分命令绑定；许可与版权以该包自身声明为准。
- Python 依赖清单：`MASTv2/requirements-v2.txt`，包括 LangGraph / LangChain、pydantic、
  numpy / scipy / scikit-image、torch / torchvision、FastAPI / uvicorn 等。各组件的许可以其发行版声明为准。
- 前端直接依赖见 `frontend/package.json`，锁定版本及传递依赖见 `frontend/package-lock.json`。
  各组件保留其自身许可，具体条件以对应发行版的许可声明为准。
- **PyMuPDF**（可选，`MASTv2/requirements-pdf.txt`）—— AGPL-3.0 / 商业双许可，作为可选依赖单独列出，
  用于文献库的 PDF 取全文与 OCR；不装时相关入口报告依赖未安装。具体许可条件以该组件的声明为准。

## 派生自第三方开源项目的代码

- **VISTA**（<https://github.com/joshhhhhan/VISTA>，MIT）：`MASTv2/mast/vista/` 的视觉脚手架（含 `runner/`）与 `integrations/claude-code/server/mast_mcp/vista_surface.py` 的工具说明文字派生自该项目，相应文件头注明了出处。其版权与许可声明原文如下：

  ```text
  MIT License

  Copyright (c) 2026 VISTA contributors

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.
  ```

- **WITecSDK**（厂商发布的 Python SDK）：`MASTv2/mast/instruments/witec/comtypes_client.py` 的连接方式参照其 COM 客户端写法，SDK 本身不随仓；许可以其发行版声明为准。

## 不随仓发布的第三方内容

- **DINOv3 骨干与 VIGIL 视觉头权重**：不随仓提供。本仓保留消费端代码（`MASTv2/mast/vision/`）；
  外部模型的使用条件以对应模型的许可声明为准。
- **OpenAlex 文献语料**（CC0）与由它派生的索引：未收录；文献库的代码在，库是空的。
- **Nanonis 软件手册与 TCP 协议文档**：厂商版权材料。读取它们的代码保留，数据不随仓；缺数据时相应功能自动降级。
- **Inno Setup 简体中文与捷克语翻译**（`ChineseSimplified.isl`、`Czech.isl`；前者来自 jrsoftware.org/files/istrans，后者随 Inno Setup 6 安装在其 `Languages\` 目录）：构建安装包时自行放入 `installer/`。
- **第三方 STM 图像**：来自他人的真实扫描图及其派生图不随仓提供。

## 借鉴与致谢

- **Scanbot**（Ceko 等）与 **Nanonis_AutoSTM**：v1 时期针尖整备与看门狗思路的来源。移植自这两个项目及其它论文公开代码的技能（`skills/paper/`，共 21 个）**未纳入本次发布**；相关项目保留各自的版权与许可声明。
- **DeepSPM、gpSTS、AtomAI、ASD-STM**：同上，思路借鉴，代码不在本仓。

如发现遗漏或归属错误，请开 issue。
