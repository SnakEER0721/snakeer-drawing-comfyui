# 第三方素材与代码

本项目的**应用代码**（`server.py`、`ui.html`、`cn_translate.py`、`poisson_blend.py`、
`paths.py`、`check_env.py`、`install_models.py`、`run_tests.py`、`启动UI.bat`、
`安装模型.bat`）、`tests/` 下的全部测试，以及四份文档（`README.md`、`CHANGELOG.md`、
`THIRD-PARTY.md`、`LICENSE`）都是自己写的，以 [MIT](LICENSE) 发布。

下面这些**数据文件**和**外部依赖**不是自己的，逐项列出出处与许可。

---

## 一、随包发布的数据

### 1. `data/danbooru_tags.sqlite3`（约 47 MB）

Danbooru 标签的中英对照表，是本应用中文翻译的**第二层词库**（第一层是
`data/base_tags.json`）。

- 直接来源：[ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table](https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table)
  的 `tag.sqlite`
- 该仓库根目录附有 **MIT License**（Copyright (c) 2026 ffdkj），MIT 允许再分发与修改
- 本应用对它做的改动：**只改了存储结构**（重建索引、按 Danbooru 分类号过滤），
  **没有改动任何标签文字**
- 标签名本身来自 [Danbooru](https://danbooru.donmai.us/) 的公开标签体系

> 附带说明：ComfyUI-Bilingual-Prompt-Inspector 项目的文档里写"上游未附带明确
> 开源许可证"，因此它自己选择不随包分发这个数据库。我们在 2026-09-18 复核了
> 上游仓库，其根目录**确实存在 MIT LICENSE**，所以本项目按 MIT 条款分发这份
> 转换结果并在此署名。

### 2. `data/base_tags.json`

第一层内置基础词库（141 条）。**这个文件的格式与初版内容来自**
[ComfyUI-Bilingual-Prompt-Inspector](https://github.com/Qiongyi44/ComfyUI-Bilingual-Prompt-Inspector)
（作者 Qiongyi44，**MIT License**，Copyright (c) 2026 Qiongyi44），
本应用在此基础上增补了 2 条。

判据：两个文件的 `description` 字段逐字节相同
（`第一版内置基础词库。个人词库优先于本词库。`）。

### 3. `高频词表.txt`、`prompt_builder.json`

本项目的原创内容资产（22 章中文词表、内置拼装选项），随本项目以 MIT 发布。

### 4. `docs/screenshot-1.png`

README 里那张界面截图。**拍的是本项目自己的界面**（出图区被故意排除在外，
免得把成品图截进去），不含第三方素材：界面上的标签列表来自上面第 1 条那份词库，
参数面板里的 LoRA 名字只是文字。

---

## 二、运行依赖（不随包发布，需用户自行安装）

| 包 | 实测版本 | 许可 |
|---|---|---|
| [Pillow](https://python-pillow.org/) | 12.2.0 | MIT-CMU License（Copyright © 1997-2011 Secret Labs AB；© 1995-2011 Fredrik Lundh 及贡献者；© 2010 Jeffrey A. Clark 及贡献者） |
| [opencv-python](https://github.com/opencv/opencv-python) | 4.13.0.92 | Apache License 2.0 |
| [numpy](https://numpy.org/) | 2.5.0 | BSD 3-Clause（Copyright (c) 2005-2025, NumPy Developers） |

`sqlite3`、`urllib`、`http.server`、`json` 等来自 Python 标准库
（Python Software Foundation License）。

---

## 三、需要用户自行安装的外部程序与模型（都不随包发布）

### ComfyUI

[ComfyUI](https://github.com/comfyanonymous/ComfyUI) 采用 **GPL-3.0**。

**本应用与 ComfyUI 之间只通过 HTTP 接口通信**（`http://127.0.0.1:8188`），
不包含、不修改、不链接 ComfyUI 的任何代码，也不以 ComfyUI 的衍生作品形式分发。
本应用是一个独立的客户端程序。

### 模型文件

本应用不附带任何模型权重。README 的「模型清单」一节给出各模型的获取方式。
使用时请遵守各模型页面自身的许可条款：

| 本地文件名 | 出处 | 许可 |
|---|---|---|
| `Illustrious-XL-v2.0.safetensors` | [OnomaAIResearch/Illustrious-XL-v2.0](https://huggingface.co/OnomaAIResearch/Illustrious-XL-v2.0) | CreativeML Open RAIL-M |
| `noobaiInpainting_v10.fp16.safetensors` | [Acly/NoobAI-Inpainting](https://huggingface.co/Acly/NoobAI-Inpainting) | 见模型页（2026-09 核对：HF 上标为 `other`） |
| `controlnet-scribble-sdxl.safetensors` | [xinsir/controlnet-scribble-sdxl-1.0](https://huggingface.co/xinsir/controlnet-scribble-sdxl-1.0) | Apache-2.0 |
| `controlnet-openpose-sdxl.safetensors` | [xinsir/controlnet-openpose-sdxl-1.0](https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0) | Apache-2.0 |
| `controlnet-union-sdxl-xinsir.safetensors` | [xinsir/controlnet-union-sdxl-1.0](https://huggingface.co/xinsir/controlnet-union-sdxl-1.0) | Apache-2.0 |
| `RealESRGAN_x4plus_anime_6B.pth` | [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | BSD-3-Clause |
| `ip-adapter-plus_sdxl_vit-h.safetensors` | [h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter) | Apache-2.0 |
| `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | [h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter) 的 `models/image_encoder/model.safetensors` | Apache-2.0 |

第一列写的是**你硬盘上那个文件名**（和 README 的「模型清单」、`安装模型.bat`
认的名字一致）。它和上游仓库名不一定相同 —— 比如上游叫
`controlnet-union-sdxl-1.0`，而这一份下到的文件带 `-xinsir` 后缀。

> ⚠️ 最后一行那个 CLIP 视觉模型**不是** `laion/CLIP-ViT-H-14-laion2B-s32B-b79K`
> 仓库里的文件。实测（2026-09）：本项目要的那份 sha256 是 `6ca9667d…`，
> 和 `h94/IP-Adapter` 的 `models/image_encoder/model.safetensors`（2.355 GiB）
> **逐字节相同**；而 laion 仓库里那份是 3.674 GiB 的 open_clip 完整检查点
> （带文本编码器），`CLIPVisionLoader` 加载不了。许可走的是 IP-Adapter 那条
> （Apache-2.0）。

> 上面 8 行**都核对过**（问的是 HF / GitHub 的公开接口，2026-09）：具体许可逐条
> 一致。想重新核对跑 `python dev/probe_model_licenses.py`（需要联网）——
> 换成官方 v2.0 做底模之后，8 行**全部**都能自动核对（v0.1 那个仓库是 401，
> 读不到许可，所以没用它）。

> **底模（`Illustrious-XL-v2.0.safetensors`）**：许可取自官方 HF 仓库的 license 标签
> （`creativeml-openrail-m`，2026-09 核对）。我们发的那份 sha256 是 `c2a1a3ea…`，
> 和官方文件逐字节相同。CreativeML Open RAIL-M **不是**随便用的许可，里面有几条
> 使用限制（见仓库里的 LICENSE），用它生成的图怎么用请自己读一遍。

---

## 四、商标

Danbooru、ComfyUI、Civitai、HuggingFace 等名称归各自所有者所有。
本项目与它们**没有隶属、赞助或背书关系**，提到它们只是为了说明数据来源和
安装方法。

生成图片中涉及的第三方角色、作品、品牌，其权利归各自所有者，
使用生成结果时请自行确认适用范围。
