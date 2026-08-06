# Issue: 流式响应原样透传 `reasoning_content`，Codex 客户端会显示原始 `<thinking>` 标签

- 日期：2026-08-05
- 严重级别：Low（仅影响显示，不影响数据）
- 状态：待处理（记录观察，非阻塞）

## 现象

Codex CLI 经 2api 代理连接上游（CodeBuddy/Qoder）时，客户端界面出现原始 `<thinking>...</thinking>` 包裹的推理文本；之前没有。

## 根因分析

- `src/qb2api/worker/streaming.py` 的 `openai_stream` 对上游 chunk 逐字透传，`delta.reasoning_content` 不作任何过滤或转换。
- 非流式聚合路径（`src/qb2api/sse.py`）会单独收集 `reasoning_parts` 到 `message.reasoning_content`，语义正确；流式路径缺失同等处理。
- 上游模型/账号近期开始返回 thinking 增量后，Codex CLI 将其渲染为原始文本。

## 建议修复（如果决定做）

在流式路径对 OpenAI 兼容端点提供开关：剥离 `delta.reasoning_content`（或按客户端协议转换为 Anthropic `thinking` block）。默认行为保持不变，避免影响依赖 reasoning 的客户端。

## 验证方法

抓取一次流式响应日志，检查 `data:` chunk 中是否含 `reasoning_content` 字段。
