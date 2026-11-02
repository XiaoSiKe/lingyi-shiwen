# 命令与采集边界

下文 `python scripts/shiwen.py` 表示使用已装好运行依赖的 Python。Agent 应使用解释器和脚本的绝对路径；脚本不改变当前工作目录。所有功能直接调用随 Skill 提供的 `wxrc/` 核心源码。

## 环境

```bash
cd "/本地/lingyi-shiwen"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/shiwen.py doctor
.venv/bin/python scripts/shiwen.py --version
.venv/bin/python scripts/shiwen.py --help
```

Windows 对应解释器为 `.venv\Scripts\python.exe`。不执行 `pip install .`，不构建项目安装包。

## 链接转 Markdown

```bash
python scripts/shiwen.py text --url "https://mp.weixin.qq.com/s/文章标识" --out "/资料/公众号" --raw
python scripts/shiwen.py text --urls "/资料/urls.txt" --out "/资料/公众号"
```

URL 文件每行一个链接，空行与 `#` 开头的行会跳过。输出 `账号/日期_标题_唯一标识.md`、`index.jsonl`；`--raw` 增加 `raw/账号/日期_标题_唯一标识.html`。HTML 为提取后的正文片段，不是完整 HTTP 响应。

## 账号识别与搜索抽样

```bash
python scripts/shiwen.py resolve --from-url "https://mp.weixin.qq.com/s/文章标识"
python scripts/shiwen.py search --name "公众号名" --since 2026-01-01 --until 2026-11-02 --text --out "/资料/搜索结果"
```

`resolve --name` 也可使用，但可能受搜索相关性、重名与收录范围影响。`search` 的 `--max-pages` 默认 6、`--per-page` 默认 10，只枚举搜索候选，不证明全量。结束日期只有日期时包含当天 23:59:59，未指定时不设该边界，时区默认 UTC+8。

## 客户端历史采集

先复制 `config.example.json` 到仓库外的私有路径。账号结构如下；值全部应来自用户自己的有效会话，不能自动生成。

```json
{
  "target": {"biz": "目标公众号标识"},
  "accounts": [{
    "name": "my-account",
    "uin": "本地会话值",
    "key": "本地会话值",
    "pass_ticket": "本地会话值",
    "wap_sid2": "本地会话值",
    "client": "ios",
    "device_seed": 20261102,
    "rpm": 6,
    "quota_hour": 40,
    "quota_day": 200
  }],
  "metrics": {"enabled": false},
  "proxies": [""]
}
```

`client` 选择实际签发会话的 `ios`、`android` 或 `pc`；空代理字符串表示用户显式选择直连。`biz` 可省略并用 `--from-url` / `--name` 解析，已配置 `target.biz` 时优先使用该值。互动数据默认关闭，确实需要时启用 `metrics.enabled`，每个实际请求均消耗账号配额。

```bash
python scripts/shiwen.py crawl --config "/私有目录/shiwen-config.json" --since 2026-01-01 --until 2026-11-02 --limit 100 --out "/资料/历史归档"
```

`--limit` 限制本次交给归档层的文章数，失败补采也计入；列表页默认上限为 50。`--dry-run` 只枚举清单；`--no-resume` 重新枚举列表，已成功归档的正文仍会去重。默认恢复同日期范围的任务；换日期范围或使用旧版游标会重新枚举，成功索引继续有效。

`crawl` 会保留失败的元信息记录，下一次运行先补采。`text` 和 `admin --text` 使用稳定文件名，但目前没有 `crawl` 的持久任务恢复队列；重复运行可能追加索引记录。不要把这些模式描述成同一套增量调度系统。

## 管理员后台

需要用户管理目标公众号。浏览器能力使用标准 Playwright，先在独立虚拟环境准备依赖与 Chromium：

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python scripts/shiwen.py admin-login --save "/私有目录/shiwen-admin.json"
python scripts/shiwen.py admin --session "/私有目录/shiwen-admin.json" --out "/资料/后台归档" --text --comments
```

用户亲自扫码。默认使用 Playwright Chromium；已有 Chrome 或 Edge 可用 `--browser-channel chrome` 或 `msedge`。登录会话文件在 POSIX 上设为 `0600`，保存到仓库外；浏览器个人资料保存在系统应用数据目录。

后台默认最多扫描 20 页，可用 `--max-pages` 调整；达到上限不能宣称扫描完整。`--comments` 只拉一页最新留言。`--dump-raw` 的后台原始响应可能含私有数据，不提交到 Git。

## 浏览器兜底

公开正文默认关闭浏览器，需要时在配置中指定：

```json
{"browser": {"engine": "playwright", "chrome": "chromium", "headless": false, "wait_human_ms": 90000}}
```

`auto` 在已安装 Playwright 时启用，`off` 关闭。只有匿名 GET 正文使用浏览器兜底，继承当前网络出口；请求导航失败会报告失败，不把空页面当作 HTTP 200。需要用户验证时使用有窗口模式，用户亲自完成验证。

## 离线解析与状态

```bash
python scripts/shiwen.py parse --file "/资料/article.html" --url "https://mp.weixin.qq.com/s/原始文章标识"
python scripts/shiwen.py snapshot --config "/私有目录/shiwen-config.json"
```

`parse` 输出结构化 JSON。`snapshot` 打印新建引擎的池配置状态，不是另一进程正在运行的实时监控。

## 停止与结果解释

退出码 `0` 表示命令按自身成功规则结束，`1` 为无结果、部分失败或业务失败，`2` 为输入/环境问题，`3` 为会话、配额、熔断或出口等导致的暂停。`crawl` 仍需查看最终 `stats.failed` 和索引状态；退出码不能代替数据完整性报告。

`status=ok` 表示正文可用，`deleted` 表示确认不可用，`meta_only` 表示只留下元信息、需要补采。互动数据同时看 `metrics_ok` 和 `metrics_reason`；数值缺省不等于真实统计为零。
