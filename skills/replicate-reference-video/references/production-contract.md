# 制作数据规范

reference-spec.json 包含参考文件 SHA-256、width、height、fps_num/fps_den、total_frames、shots、text_events、audio_events、transitions、观察依据和工具版本。时间用输出帧，左闭右开；源素材区间用秒。

每个 shot 记录 id、start_frame/end_frame、subject、action、shot_size、scene、direction、camera、composition、lighting、color、evidence。允许具体主体外观变化，保留分镜语义和制作约束。叠化另记图层重叠规则。

text_events 独立于镜头，允许跨镜头。记录 text、start_frame/end_frame、font_name、font_file、font_status（verified/inferred/unknown）、size_px、weight、归一化位置和文本框、对齐、颜色、描边、阴影、行距、动画及依据。未知值填 null，不可推测成确认。

audio_events 记录 kind（voice/music/sfx）、区间、台词、音源、音色描述、增益、淡入淡出和核验依据。transitions 记录切点、类型、时长、涉及图层。

match-plan.json 格式如下。此例仅说明格式，不是已验证素材：

```json
{
  "fps_num": 30, "fps_den": 1, "total_frames": 90,
  "shots": [{
    "id": "s01", "start_frame": 0, "end_frame": 90,
    "status": "matched", "clip_id": "m01",
    "src_in": 1.0, "src_out": 4.0, "clip_s": 10.0,
    "checks": {"subject": true, "action": true, "shot_size": true,
      "direction": true, "camera": true, "composition": true,
      "scene": true, "place": true},
    "evidence": "填写实际观看该片段的时间与观察记录"
  }]
}
```

使用 matched / transform_required / gap，后两种表示未解决，不可通过验收。默认 speed=1，不循环、不冻结；修改速度需要单独评审。历史素材可能缺少时长，须先 ffprobe 实测，再填 clip_s。素材 ID 和区间不可编造。

```bash
python3 <skill目录>/scripts/check_plan.py <任务目录>/match-plan.json --inventory <任务目录>/library.json
```

结构检查只验证时间、ID、可用文件和声明的硬条件，不能判断观看证据是否真实。最终报告对结构、画面、字幕、字体、声音、转场分别记录 pass/gap/unverified，附参考与输出对照依据。切点默认允许一个输出帧误差；这是工程默认阈值，不是参考实测数据。字体不明或音色不同必须作为差异披露。
