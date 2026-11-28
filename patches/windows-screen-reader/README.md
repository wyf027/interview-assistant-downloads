# Windows 读屏练习补丁

适用于 Windows 版面试助手 0.2.0-beta.2 题库试用版。用于在公开模拟题页面上练习。补丁包含 Electron 打包后资源的文本差异、独立 Python/PowerShell 读屏脚本，以及两组格式回归检验。

- 自动跟踪焦点和鼠标所在题目，切换时复用同一套题的答案缓存。
- 支持单选、多选、填空、问答和编程题的短答案展示。多选保留题面选项顺序，填空保留空位编号。
- 紧凑答案窗按内容调整大小，鼠标可点击后方练习页面。字体与背景透明度分别设置。
- 本地模式使用 llama.cpp。应用内配置 API 后，也可主动切换到该服务商。仓库中的补丁不含 API Key、练习记录、模型或安装包。

## 在独立 Windows 测试副本中试用

准备 Git、Python 3.14 和 Pillow，以及从原版安装包提取的 Windows 测试副本。系统 OCR 需要对应的 Windows 语言包。可选的 RapidOCR Python 包及其依赖放在 `reader/local/image-ocr` 以辅助识别图片。对于本地模型，将可用的 Windows 版 `llama-server.exe` 和它的运行库放在 `reader/local/runtime`，将匹配的模型放在 `reader/local/Qwen3.5-4B-Q4_K_M.gguf`。

在补丁目录中使用 `python build_archive.py <原版 app.asar> <新 app.asar 路径>` 生成新包。脚本校验原有资源和补丁结果的 SHA-256。将新包放入测试副本的 `resources/app.asar`，并将 `reader` 目录放入测试副本的 `resources`。默认使用 PATH 中的 `python.exe`，可在启动测试副本前通过环境变量 `MC_READER_PYTHON` 指向解释器，或通过 `MC_READER_DIR` 指向其它读屏脚本目录。

用 `python -B reader/tests/choice.py` 和 `python -B reader/tests/fill.py` 运行格式回归。原版应用是 [JWM0203/MeetingCopilot](https://github.com/JWM0203/MeetingCopilot) 的 Apache 2.0 衍生发行版，应用原有许可与声明继续随原版安装包提供。当前补丁仍处于试验阶段，本地模型生成的答案准确率有限。
