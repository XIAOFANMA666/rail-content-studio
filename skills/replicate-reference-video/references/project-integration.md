# 轨道项目接入

2026-11-14 本地源码核对；运行时再次读取当前源码和 AGENTS.md。项目路径通过参数传入。

新增本地入口 `/#reference`，由 reference_video.py / reference_http.py 执行导入、按 Skill 分镜分析、素材匹配、缺口补库和隔离 MP4 渲染。具体能力与未支持效果见项目 docs/reference-video.md。新增入口保存原音轨、支持参考画幅与时长和静态字幕；自动分析是抽样推断，最终仍须对照验收，不能将 preview_ready 当作完全一致。以下旧入口限制仍然成立。

| 能力 | 实际入口 | 限制 |
| --- | --- | --- |
| 优先旧素材库 | reference_legacy_media.approved_videos / export_approved_videos | 只读真实已审区间，保留原来源和授权；图片不能充当运动镜头 |
| 历史库 | data/library.json 的 clips | 检查引用文件存在，不能补造审批记录 |
| 正式库 | media_library.approved_clips(root,out) | 仅 approved，过滤缺失文件；停用素材不得新增使用 |
| 合并库 | media_library.merge_library(root,out,legacy) | 历史与正式素材，无向量检索 |
| 查询 | /api/media/list、/api/media/detail、/api/media/clips | clips 仅新增正式素材，不等同全库 |
| 缺口优先搜索 | reference_legacy_media.search(request) → 旧库 /api/commons/start | video 使用旧 Agent，内部最多3轮；image 只用于明确静态需求并使用 fixed |
| 项目补充搜索 | POST /api/media/search → Service.submit("search",body) | 已配置的 Pexels 来源；需密钥，最多三组词、二十候选、自动分析五条；与旧库共用每镜头2任务预算 |
| 下载与分析 | POST /api/media/analyze → Service.submit("analyze",body) | 依赖 FFmpeg/ffprobe 与画面模型；抽样八帧，不等同全片确认 |
| 审批入库 | POST /api/media/decide → Service.decide(body) | 由用户人工审批，Skill 不冒充审批人 |
| 跟踪任务 | GET /api/media/tasks、GET /api/media/detail | 读取真实任务和候选状态，不把提交任务当完成 |
| 召回 | pipeline.retrieve(line,clips,durations,min_clip_s=0) | 标签、地点、时长不能证明景别与动作 |
| 配音 | pipeline.tts；llm.model_config | 发生系统语音回退时标记音色差异 |
| 草稿 | pipeline.write_draft(plan,skill,clips,draft_root,draft_name) | 固定1080×1920、30fps；字幕全局参数；会覆盖 output/jianying_draft，禁止对现有任务直接调用 |
| MP4 | roughcut_export.render(root,out,result,swaps=None) | 固定竖屏30fps和Arial；无通用转场/BGM；会截断offset和补尾帧，不可冒充精确复刻 |
| 检查 | draft_review.py；tools/distill_skill.py | 引用、覆盖与统计检查，不能证明画面语义或字体一致 |

导出素材候选清单：

```bash
python3 <skill目录>/scripts/library_inventory.py --project <项目目录> --output <新任务目录>/library.json
```

脚本只读取已有库，不搜索、下载、审批或初始化数据库。使用项目 Python 环境。标签仅供召回，必须实际查看候选片段。

库存不足时使用上述项目任务接口补充。搜索 body 对照当前 media_library.validate_request：query、place、queries（最多三组英文词）、orientation、min_width、min_duration、auto_analyze、context。在 context 记录镜头 ID 和硬约束，min_duration 至少为镜头所需时长。分析和审批请求字段须读取当前实现，不猜字段。HTTP 写请求受本地 Host/Origin 校验，按工作台正常入口调用，不绕过校验。补库会按用户要求写入素材库任务和候选记录，但不得覆盖既有工程或伪造审批记录。

在独立任务目录用 FFmpeg/ASS 或已安装剪辑库实现参考所需时间轴、字体与声音。不要直接复用不支持这些参数的导出器；不能实现时输出具体阻塞，不声称完成。用户明确要求打通功能时可修复业务代码并验证；不要覆盖用户已有工程。

扩展素材库接入：reference_legacy_media.py 通过 http://localhost:8765 的真实任务接口执行搜索，config 检查模块/配置存在性，search(kind="image"|"video") 提交，task 读取真实状态。Commons 支持图片和视频，Archive 为视频补充来源；图片不能导出为运动镜头。export_approved_videos 只导出审批范围内视频片段，保留来源和授权。配置存在不代表网络已验证；必须查看任务候选、errors 与 stopReason。

## 运行时素材搜索计划

读取 `material-search-policy.json`，不要在调度代码另写一份不同的轮次和重试数字。规划器为纯计划阶段：

```bash
python3 <skill目录>/scripts/plan_material_search.py \
  --analysis <任务目录>/analysis.json \
  --technical <任务目录>/technical.json \
  --output <任务目录>/material-search-plan.json
```

Python 接口为 `validate_policy(policy)`、`build_plan(analysis, technical, policy)`；前者返回校验后的副本，后者不访问网络、不修改任何库。修改第二次查询后用 `fingerprint_request(request, requirements, policy)` 重算指纹，完整硬条件保持原样。不要复用旧指纹代表新查询。

`technical` 需要 width、height、total_frames 和帧率，优先提供 fps_num/fps_den 或 fps_rational；只有数值 fps 时按其原值计算，不假称找回被舍入的原帧率。每镜头的 `duration_frames` 和 `duration_seconds_exact` 保留精确有理数，HTTP duration 仅用于召回，连续素材区间须按输出帧验证。

`analysis.shots` 沿用逐镜头语义字段；`queries` 提供英文完整检索描述，基础英文 tags 可作后备召回词。`motion_required=false` 或 `static_requirement=true` 是允许静态图像的明确声明，未知运动需求默认 video；与 `motion_required=true` 冲突的 image 声明必须处理，不能静默使用图片。

后验规则按 `applicable_media_type` 区分：video 需要连续源区间、正常速度且禁止循环/定格；明确静态的 image 使用 `static_hold_allowed=true`，按时间轴展示帧数验证，不携带不适用的连续视频/速度字段。静态展示不能用于替代运动镜头。

每镜头输出包含：

- `request`：可传给旧库桥接器的 query、keywords、kind、place、required、duration、orientation、purpose、mode。query 保留完整语义和制作约束；keywords 仅取短英文召回词；orientation 留空，允许高分辨率源裁切。
- `requirements`：完整条件、帧区间、精确时长、输出画幅、地点和最终裁切后的核对项；原始镜头保存在 `original_requirement`。
- `request_fingerprint`：request、完整 requirements、policy SHA-256 的规范 JSON SHA-256，跨恢复运行保持稳定；不含当前时间。
- `provenance`：策略ID/摘要、项目嵌入版 Skill 文件摘要和摘要文字，另保留原始需求摘要。`status=blocked` 时不可提交；`ready_for_inventory_check` 仍须先查库存。

调度器负责持久化 `request_fingerprint → task_id/状态/原始request/requirements`。提交前保存预留记录；重启后先复用已提交、运行、等待、完成、失败或取消的记录。请求响应丢失时核对旧库任务及 payload 后再决定，不能重复创建。策略允许的第二个外部任务只用于成功执行后的 no_results/no_match，并需依据实际缺口调整查询；网络重试不创建新任务。

旧库恢复入口为 `POST /api/commons/resume-search`，body 为 `{"id": task_id}`。读取旧任务 `result.network_retries`、`cooldown_until` 和 HTTP Retry-After；本次延迟为 `max(min(base_delay_seconds * multiplier**retry_index, max_delay_seconds), 服务端剩余等待)`，retry_index 从0开始。服务端等待是下限，不能截短；超过总等待预算时保留任务与下次可恢复时间，报告延后，禁止紧密轮询。有限次数/等待约束见策略JSON。

`no_results` 只能表示请求成功且返回零候选；有候选但全不符为 `no_match`。包含网络或来源错误时按 failed/rate_limited 保留原因；有候选但未真实审批为 pending_approval。不得仅凭任务成功、标签相似或文件存在改成 matched。
