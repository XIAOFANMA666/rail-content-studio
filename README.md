# 轨道 · 内容工作台

面向出海火车票短视频业务的 AI 粗剪工作台。本仓库包含新版三栏剪辑界面、日间／暗黑模式、成片评测入口，以及按作品查看指标的数据复盘页。

[在线演示](https://xiaofanma666.github.io/rail-content-studio/) · [来源仓库](https://github.com/ethan17461/ai-roughcut-demo)

## 拉取并查看（无需密钥）

需要 Git 和 Python 3.9 或更新版本。仓库包含 53 段示例视频，完整下载约 160 MB。

```bash
git clone https://github.com/XIAOFANMA666/rail-content-studio.git
cd rail-content-studio
python3 studio.py
```

打开 <http://localhost:8770>。首次启动自动加载随仓库附带的运行结果，可以播放、搜索素材、预览原片、替换预览镜头和查看复盘，无需安装模型依赖或填写密钥。Windows 如没有 `python3` 命令，使用 `python`。

8770 被占用时，macOS / Linux 使用 `PORT=18770 python3 studio.py`，然后打开 <http://localhost:18770>。按 Ctrl+C 停止服务。

## 生成自己的粗剪（macOS）

完整流水线使用 macOS 的 `afconvert`；没有 MiniMax 配音服务时会使用 `say`。Windows / Linux 可查看演示，完整生成流程尚未在这两个系统验证。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

在本机 `.env` 中填写模型服务密钥，再运行：

```bash
python studio.py
```

- 写脚本：配置 `DEEPSEEK_API_KEY`，或 `SCRIPT_BASE_URL`、`SCRIPT_API_KEY`、`SCRIPT_MODEL`。
- 配音：配置 `MINIMAX_API_KEY`；未配置时回退到 macOS 系统语音。
- 成片评测：配置支持图像输入的模型，填写 `VIDEO_BASE_URL`、`VIDEO_API_KEY`、`VIDEO_MODEL`。兼容格式见 `.env.example`。
- 在“选题与脚本”页输入选题，点“生成脚本”；三版中英文脚本直接显示在页面上，生成脚本不会改变现有预览或配音。
- 点击某版的“选用此版”，再点“生成配音与粗剪”。制作复用选中的脚本，不再次调用脚本模型。刷新页面可恢复最近生成的脚本。
- 替换镜头后点“写入剪映”，再在剪映中精修。
- 模型调用会产生费用。成片评测会发送抽取画面和转写文本；本地语音识别首次使用还需下载模型。

`.env`、本机生成结果与虚拟环境均不提交。每位使用者配置自己的密钥。

## 在剪映专业版中打开

1. 在本地工作台生成配音与粗剪。程序会创建包含素材、配音、字幕和花字的工程文件夹；若换过镜头，先点击“写入剪映工程”保存修改。
2. 点击“素材与粗剪”页顶部的“导入剪映指引”，查看本次工程名称与真实路径。默认位于 `~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft/`。
3. 打开剪映专业版，在草稿列表中查找同名工程。剪映已打开时，完整退出后重开；仍未出现时，核对剪映设置中的草稿目录，将**整个工程文件夹**复制到实际目录，保留 `materials/`，不要只复制 JSON。
4. 在剪映检查画面、声音和字幕，精修后使用剪映的“导出”输出 MP4，再上传到工作台做成片评测。

工作台生成的是可编辑工程，不直接导出 MP4。工程文件和素材引用已核对；剪映客户端内的打开与导出尚未实测，不同版本的兼容性仍需验证。

## 当前范围

| 页面 | 可用内容 | 边界 |
| --- | --- | --- |
| 选题与脚本 | 输入选题、生成三版脚本、比较并选用、生成配音与粗剪 | 实际生成需要本地服务和模型配置；脚本仍需人工核对 |
| 素材与粗剪 | 素材搜索与原片播放、竖屏预览、候选镜头替换、四轨定位和缩放 | 时间轴不支持拖拽裁剪；首次拉取的示例只能预览替换，生成本机草稿后才能写入剪映 |
| 成片评测 | 上传 MP4 / MOV / M4V，查看指标及人工复核项 | 最长 3 分钟、最大 100 MB；需要本地依赖与模型配置 |
| 数据复盘 | 平台与状态筛选、列表／作品墙、单作品指标弹窗 | 发布记录和播放数据为模拟数据；未关联成片的作品不能播放 |

在线演示为静态回放，不调用模型、不上传评测、不写入本机剪映。随仓库附带的 `draft/draft_info.json` 是工程结构参考，素材路径经过处理，不能保证直接导入；可用工程请在本机重新生成。

## 继续修改与验证

界面源文件为 `web/template.html`。只更新界面并保留已有演示数据：

```bash
python3 build_web.py --ui-only
```

完整生成后用 `python3 build_web.py` 更新示例运行及媒体。不要混用不同运行的 `index.html`、配音和工程文件。

安装依赖后的测试：

```bash
python -m unittest discover -s tests -v
```

后续获取本仓库更新：

```bash
git pull --ff-only
```

公开仓库任何人都可拉取；推送修改需要仓库写权限，也可 Fork 后提交 Pull Request。

## 来源与验证范围

本版基于 `ethan17461/ai-roughcut-demo` 的提交 `b665263ea92dc8b0c911c9b89d7532fb83570b9d`，保留原流水线及贡献归属，并加入本次工作台界面改版和首次拉取启动支持。原仓库提到的分叉贡献来自 `yubb6688-alt/ai-roughcut-demo`。素材来源链接保留在 `data/library.json` 与页面素材预览中；剪映工程使用 [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)。本仓库不新增对上游代码或第三方素材的授权承诺。

本次发布检查覆盖界面交互、单元测试及无密钥首次启动；未重新调用付费模型或验收剪映真实导入。`docs/evidence/` 和 `AGENTS.md` 保留上游历史记录，其中旧界面、旧链接、实测数值与能力说明须按其原始版本理解；本版启动方式与交付范围以本 README 为准。
