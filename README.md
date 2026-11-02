<p align="center">
  <img src="assets/logo.svg" width="80" height="80" alt="零一拾文标志">
</p>

<h1 align="center">零一拾文 · 公众号自适应采集引擎</h1>

<p align="center">
  把散落的文章，拾成可复用的内容档案。
</p>

<p align="center">
  <img alt="Skill + Source" src="https://img.shields.io/badge/Skill_+_Source-176B60?style=flat-square">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3B5365?style=flat-square">
  <img alt="Version 0.1.9" src="https://img.shields.io/badge/version-0.1.9-B3925B?style=flat-square">
</p>

<p align="center">
  初步雏形开发者：<strong>Spark</strong><br>
  <sub>本项目在 Spark 的采集核心雏形上，完善可靠性与 Skill 调用流程。</sub>
</p>

<p align="center">
  <a href="#-开始使用">开始使用</a> ·
  <a href="SKILL.md">Skill 入口</a> ·
  <a href="references/commands.md">命令参考</a> ·
  <a href="references/architecture.md">核心设计</a>
</p>

---

## ✦ 让 Agent 替你整理文章

对支持本地 Skill 和命令执行的 Agent 说：

> 用零一拾文，把这些公众号文章保存成 Markdown，按账号和日期整理，并告诉我哪些还没成功。

**零一拾文是一个自带核心源码的 Skill。** Agent 根据任务选择采集模式，再通过 `scripts/shiwen.py` 调用 `wxrc/`。源码随 Skill 一起提供，无需安装项目软件包。

<table>
<tr>
<td width="33%"><strong>⌁ 收集</strong><br><br>从文章链接、搜索结果、客户端历史或管理员后台取得内容。</td>
<td width="33%"><strong>◈ 整理</strong><br><br>输出 Markdown、正文 HTML 与 JSONL 索引，保留来源和时间。</td>
<td width="33%"><strong>↻ 恢复</strong><br><br>待处理队列、成功去重和失败补采，让中断后的任务接着完成。</td>
</tr>
</table>

## ◇ 先选对采集方式

| 你手上有什么 | 使用方式 | 可获得的内容 | 范围说明 |
|---|---|---|---|
| 一篇或一批文章链接 | `text` | 正文、图片链接、基础元信息 | 无需客户端凭证；受文章可访问性影响 |
| 公众号名称 | `search` | 搜索命中的文章，可按日期筛选 | 搜索抽样，不能保证全量 |
| 客户端会话配置 | `crawl` | 客户端可见历史、正文，可选互动数据 | 受会话有效期、配额和可见范围限制 |
| 自己管理的公众号 | `admin` | 后台发表记录，可选正文、最新留言 | 需要管理员登录；有翻页上限，互动字段随响应而定 |
| 已保存的文章 HTML | `parse` | 离线结构化解析 | 不访问网络 |

阅读数、点赞数、留言需要相应会话或后台权限。未获得的数据应结合状态字段判断，不能把默认值当成真实的零。`admin --comments` 当前获取一页最新留言，并非全部历史留言。

## ↗ 开始使用

### 1 · 把源码作为 Skill 放好

以 Codex 的本地 Skill 目录为例：

```bash
git clone https://github.com/XiaoSiKe/lingyi-shiwen.git "$HOME/.codex/skills/lingyi-shiwen"
cd "$HOME/.codex/skills/lingyi-shiwen"
```

其他 Agent 可将整个仓库放入其支持的 Skill 目录，或按其方式加载根目录 `SKILL.md`。**保留完整目录**，因为入口会直接调用旁边的核心源码。

### 2 · 准备独立运行环境

需要 **Python 3.10+**。以下步骤只安装运行依赖，不安装本项目、不构建安装包。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/shiwen.py doctor
```

Windows 使用 `.venv\Scripts\python.exe` 替换 `.venv/bin/python`。`doctor` 离线检查 Python、运行依赖和可选 Playwright 依赖。

### 3 · 用自然语言调用

重新加载 Agent 的 Skill 列表后，输入：

```text
使用 $lingyi-shiwen，把这些链接保存为 Markdown，输出到我指定的资料目录。
```

也可以直接执行同一个源码入口：

```bash
.venv/bin/python scripts/shiwen.py text \
  --url "https://mp.weixin.qq.com/s/文章标识" \
  --out "/你的资料目录/公众号文章" --raw
```

更多用法，包括批量链接、日期筛选、客户端会话配置和管理员扫码，见 [命令参考](references/commands.md)。

## ▤ 内容如何保存

`crawl` 模式按账号、月份归档。文件名包含文章唯一标识，避免同名内容覆盖；图片采用稳定命名。

```text
archive/
├── 公众号名称/
│   └── 2026-11/
│       ├── 2026-11-02_文章标题_唯一标识.md
│       ├── raw/                  # 正文 HTML
│       ├── images/               # 启用图片下载时保存
│       └── comments/             # 取得留言时保存
├── index.jsonl                   # 归档状态与来源索引
└── cursor.json                   # 列表游标与待处理队列
```

`text` 模式保存为 `账号/日期_标题_唯一标识.md`；`--raw` 将正文 HTML 保存到 `raw/账号/`。两种模式的输出布局详见命令参考。

## ↻ 持续完善

**v0.1.9**：浏览器支持统一为 Playwright；修复令牌桶重复记账、长文章 JSON 截断、部分失败退出码和互动数据 HTTP 错误判定。


- **恢复更完整**：先保存待处理列表，下游处理完成后确认；页内限额不会丢掉本页剩余文章。
- **失败可补采**：只有成功归档或确认删除的文章进入完成索引；`meta_only` 在后续运行重试。
- **文件不冲突**：正文、正文 HTML、留言和图片均使用稳定标识，减少同名覆盖。
- **判断更准确**：正文关键词与验证页分开识别，HTTP 5xx 按服务错误处理。
- **账号策略统一**：列表与互动数据请求共用身份绑定、逐请求计费、单账号限速和冷却规则。
- **暂停可预期**：重试中逐次检查熔断；配置的出口不可用时停止，不创建隐式直连。
- **登录态更收敛**：会话文件原子写入并在 POSIX 上设为 `0600`，日志隐藏会话 token 与敏感参数。

这是源码与 Skill 工作流迭代。没有桌面安装程序、项目 wheel 或打包发布步骤。

## ⌘ 源码结构与验证

```text
lingyi-shiwen/
├── SKILL.md                      # Agent 读取的操作流程
├── scripts/shiwen.py              # 与工作目录无关的源码调用入口
├── wxrc/                         # 唯一一份 Python 采集核心
├── agents/                       # Codex 展示信息
├── references/                   # 命令、边界、架构与来源
├── tests/                        # 本地模拟与回归用例
└── config.example.json           # 无凭证配置样例
```

开发验证直接运行源码：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

测试覆盖本地模拟采集与恢复逻辑。浏览器适配层使用离线模拟验证；真实页面样本缺失时，相应用例会跳过；本地通过不代表微信线上长期采集效果已获验证。

## ♧ 致谢与项目来源

**Spark 是本项目初步雏形的开发者。** 原始采集核心提供了多通道采集、正文解析、账号与出口管理、自适应节流，以及浏览器桥接的基础。本次迭代在其基础上修复可靠性问题，并加入直接调用核心源码的 Skill。

来源与版本记录见 [项目来源](references/origin.md)。浏览器兜底与管理员扫码统一使用标准 Playwright。需要时安装 `requirements-browser.txt` 并运行 `python -m playwright install chromium`，也可以选择本机 Chrome 或 Edge。

仓库目前未指定开源许可证。公开源码便于查看与协作，不代表已授予任意再分发或商业使用授权。
