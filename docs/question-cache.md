# 划线问 AI 的上下文缓存

实现及验证日期：2026-10-19。

每次仍发送当前划线的完整历史，不用裁剪历史换取缓存命中。请求排列为：固定系统提示 → 固定书页文字、原图及知识快照 → 按顺序追加的完整对话 → 当前问题。

## 固定前缀

SQLite 的 `question_prefixes` 表保存每处选文首次使用的图文内容。键包含书籍内容 ID、页码、去空白的选文、模型、思考配置、网关地址及系统提示版本，不包含当前问题、时间或随机数。首次写入由数据库原子确定，重启后继续复用；后续后台知识库更新不会改写这处对话的前缀。PDF 原图只渲染一次。

历史回答保留模型原始文本，包括首尾空白，不重新排版或总结后再发送。停止或失败的回答注明未完成；其他划线的对话不会混入。新的用户消息和回答只追加到末尾。

## 模型服务端缓存

DeepSeek 自动管理上下文缓存。其 Responses 接口不支持通过 `prompt_cache_key` 或 `prompt_cache_retention` 强制开启缓存，因此这里不发送无效控制参数。[DeepSeek Responses 兼容说明](https://api-docs.deepseek.com/guides/responses_api/)

稳定前缀提供可复用的输入，但首次请求、缓存淘汰和上游路由等情况仍可能不命中。没有为了显示命中而重复发送相同请求或插入无用文本。

LangChain 流式结束时返回的 `usage_metadata.input_token_details.cache_read` 是命中统计来源。后端在 `done.usage` 中返回 `input_tokens`、`output_tokens`、`cached_tokens`、`prefix_reused` 及前缀摘要。`prefix_reused` 仅表示本地快照复用，不能作为模型服务命中的证据。[LangChain 缓存统计说明](https://docs.langchain.com/oss/python/integrations/chat/openai)

界面区分实际命中、服务返回零命中、首次冷请求及未返回统计。未知统计保留为空，不伪装为零或命中。

## 真实三轮验证

使用默认 DeepSeek Flash、`effort=none`、网关 `/responses`，输入测试书第 55 页文字及原图，第二轮与第三轮带上此前完整对话。

| 轮次 | 输入 token | 命中 token | 本地前缀复用 |
| --- | ---: | ---: | --- |
| 第一轮 | 1,454 | 0 | 否 |
| 第二轮 | 1,492 | 1,408 | 是 |
| 第三轮 | 1,532 | 1,408 | 是 |

前缀摘要三轮一致；两次追问均保留前面的回答并命中上游缓存。这是本次请求返回的统计，不保证未来每次命中。原始记录位于本地忽略目录 `tmp/ask-cache-real/result.json`，不随仓库上传。

自动化验证覆盖真实 LangChain 请求序列化、下一轮输入保留上一轮全部消息前缀、知识更新及服务重启后快照不变、图片不重复渲染，以及缓存用量从流式响应到界面的传递。
