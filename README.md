# 清单取件 · List Pick

<img src="assets/list-pick.svg" width="80" alt="清单取件图标">

按清单核对、选择并复制本地文件的 Windows 离线工具。适合已经拿到资料名单，需要整理交付文件、确认缺失和同名候选的场景。

它聚焦清单与复制报告；日常全盘文件搜索推荐使用 Everything。本项目不声称在搜索速度或搜索功能上胜过 Everything。

## 下载与使用

1. 在 [Releases](../../releases) 下载 Windows ZIP，完整解压后运行 `清单取件.exe`。
2. 点击“加载演示”体验虚构资料，或粘贴 Excel/WPS 中的一列文件名。
3. 选择来源目录和匹配模式，扫描后确认候选。
4. 选择来源目录以外的输出位置，确认复制数量，生成文件副本及 CSV 报告。

当前版本为 **0.2.1 试验版**。无需安装 Python、Excel 或 WPS。没有账号、联网、广告或支付逻辑。

## 功能

1. 完整文件名、不含扩展名、正则表达式和最终后缀匹配。
2. 包含/排除后缀、子目录范围和大小写选项。
3. 按原清单记录唯一命中、缺失、多候选、重复和无效输入。
4. 候选内搜索与显式勾选；相同路径跨规则命中只复制一次。
5. 保留相对目录、原文件不变、创建新输出目录，生成核对与复制明细报告。
6. 正则超时保护、取消任务、输入修改后旧结果失效保护。

结果区搜索只影响显示，不改变已确认的复制计划；候选窗口的勾选才改变人工选择。复制前会显示总计划数量。

## 开发与构建

Windows，Python 3.12，包含 Tkinter：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe app.py
```

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v
.\.venv\Scripts\python.exe validate_demo.py
.\.venv\Scripts\python.exe app.py --smoke-test
.\build.ps1
.\.venv\Scripts\python.exe package_release.py
```

`build.ps1` 生成单文件 EXE；打包脚本默认写入 `release/`，已存在同版本目录时停止。`--smoke-test` 检查窗口、内置图标和演示、正则/后缀扫描及虚构文件复制。图标源文件在 `assets/`；重新生成 PNG/ICO 可安装 Pillow 后运行 `build_assets.py`。

## 验证与边界

1. 本地最终构建运行 78 项检查，77 通过，1 项真实符号链接测试因 Windows 权限跳过；独立 EXE 烟测通过。
2. GUI 事件验收曾出现一次瞬时失败，单项及最终全套重跑通过；欢迎提供可复现案例。
3. 当前只在开发电脑验证；尚未完成其他电脑及 Windows 11 验收，程序未代码签名。字体缩放布局测试不等同于完整视觉验收。
4. 不处理 OCR、文档内容识别、压缩包内部搜索或业务模板。直接读取 TXT，支持从表格复制一列。
5. 扫描不是目录快照；扫描后文件可能变化。失败或取消时可能保留部分输出，请查看完成提示和报告。
6. CSV 对可能成为公式和纯数字清单名称加英文单引号文本标记，部分阅读器会显示该标记；源文件名不变。

报告问题时请使用虚构文件，提供版本、复现步骤和错误文字，不要上传私密资料。

## 许可证

项目原创源码及图标采用 [MIT](LICENSE) 许可证。随发行版捆绑的 Python、Tcl/Tk、PyInstaller 和 regex 适用各自许可证，见 [third-party-licenses](third-party-licenses)。
