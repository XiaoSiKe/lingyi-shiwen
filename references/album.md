# 指定合集自动采集

`album` 直接调用零一拾文核心源码：合集列表 → 自动分页 → 成员去重 → TextRun 正文解析与保存。无需另外的采集项目或安装本项目软件包。

## 指定目标

使用合集分享链接（路径为 `/mp/appmsgalbum`，包含 `__biz` 和 `album_id`）：

```bash
python scripts/shiwen.py album \
  --url "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=公众号标识&album_id=数字合集ID" \
  --expect-title "零一文章" \
  --out "/资料/零一文章合集" --raw
```

也可以提供两个明确标识：

```bash
python scripts/shiwen.py album --biz "公众号标识" --album-id "123456789" --out "/资料/合集"
```

合集名称或公众号名称不足以唯一定位合集；普通单篇文章链接也可能属于多个合集，所以该命令不会猜测归属。`--expect-title` 可选，用于避免抓错合集。

## 自动翻页与完成条件

每页请求 20 条，按返回的最后一项 `msgid/itemidx` 继续翻页；同一文章按两者组合去重。默认不限制正文处理数量，分页安全上限为 10,000 页。可用 `--max-pages` 调整，或者用 `--max-articles` 限制本次新处理的正文数（0 不限制，已存在正文不消耗该额度）。

只有接口明确返回列表结束，并且已发现的唯一文章数与可解析的声明总数一致时，才标记列表完整；正文还必须全部成功，才标记 `archive_complete=true`。缺少可解析总数时，列表完整仅依据接口结束标志，`reported_total` 如实保留。

异常响应不会被解释为空合集。接口被拦、凭证问题、重复游标、页数上限、条数上限、总数不符和正文失败都会阻止“全部归档成功”的结论。`cursor_stalled` 表示分页游标不再前进，`count_mismatch` 表示总数不符。

合集内容可能在采集过程中变化，因此“完整”限定为本次接口声明的可见成员范围，不涵盖不可访问、付费受限或平台未返回的内容。

## 恢复与刷新

- 使用原命令和同一输出目录：恢复未处理文章，重试失败正文，核验并复用已保存文件。
- `--list-only`：只获取完整成员清单；下次去掉该参数可继续下载正文，不需要重新翻页。
- `--restart`：重新枚举当前合集成员，复用正文成功索引。适合检查新增文章或在合集变更后重新核对数量。旧正文文件保留，不自动删除。
- `--raw`：同时保存正文 HTML；设置随任务保存，恢复后继续使用。
- 一个输出目录绑定一个合集，传入不同目标会拒绝执行，避免混写。
- 若接口持续返回异常，保留状态并停止；重新运行不应被用于绕过访问限制。

## 输出

- `album-state.json`：目标标识、游标、成员状态与恢复选项。
- `album-index.json`：文章清单、链接、标题及逐项结果。
- `目录.md`：带原文链接与状态的合集目录。
- `collection-report.json`：列表数量、声明总数、归档成功/失败/待处理数量、停止原因与完整性。
- `index.jsonl` 与按账号保存的 Markdown、可选 HTML：复用既有归档格式。

正常完成返回 0；有限额、总数不符或正文失败等部分完成返回 1；输入/响应格式问题返回 2；引擎熔断等暂停返回 3。无论退出码如何，均以报告字段判断实际完成范围。

## 协议参考与验证边界

字段和分页参数参考公开项目 wechat-article-exporter 的固定提交 `6124c4d142eb1856ef47895f63932cfcdd385671`：

- [合集请求参数实现](https://github.com/wechat-article/wechat-article-exporter/blob/6124c4d142eb1856ef47895f63932cfcdd385671/server/api/web/misc/appmsgalbum.get.ts)
- [合集响应类型](https://github.com/wechat-article/wechat-article-exporter/blob/6124c4d142eb1856ef47895f63932cfcdd385671/types/album.d.ts)
- [分页消费逻辑](https://github.com/wechat-article/wechat-article-exporter/blob/6124c4d142eb1856ef47895f63932cfcdd385671/pages/dashboard/album.vue)

本模块为独立实现，参考的是公开接口的字段与调用约定，没有复制该项目的业务代码。这不是微信官方稳定 API 文档，平台调整后可能需要适配。

本版本完成的是离线模拟及 CLI→核心源码→文件产出的契约测试；未访问被浏览器工具策略禁止的站点，也未实际采集用户的公众号合集。不能把这些测试描述为线上合集已验证成功。
