# 3.7 证券搜索与网站目录

底座对外提供 `GET /api/v1/search`，从当前已发布选股池搜索证券。底座旧路径 `/api/data/search` 也返回相同的证券契约。InvestMind 网站负责匹配功能页面并根据证券代码生成工作台链接。需要原先“证券＋功能页面”响应的调用方应访问**网站域名**下的 `/api/data/search` 或 `/backend-api/api/data/search`。网站搜索页 `/search` 复用同一逻辑。

## 证券 API

| 参数或返回字段 | 口径、来源及限制 |
| --- | --- |
| q | 可选关键词，默认空串。先做 Unicode NFKC 归一化、去首尾空白，再判长度与匹配；默认最多 64 个 Unicode 字符。返回 query 保留归一化后的大小写。 |
| page、pageSize | 均为正整数；page 默认 1。每页默认 20、上限 50，部署可在底座配置中调整。未知键、重复键、非法整数、超限参数返回 400，无数据库读取。 |
| stocks.items[].symbol | 证券主表的 ts_code，经当前选股快照发布为证券代码，规范为六位数字加 .SH/.SZ/.BJ；选股池明确了可见范围。 |
| stocks.items[].code、name | code 为 symbol 的六位代码部分。name 为同一选股行发布的证券名称，原始来源是 Tushare stock_basic → core.security.name。没有新增采集任务、历史表或搜索专用证券副本。 |
| stocks.status、items、total | 空关键词为 idle，总数空；非空查询从 ready 的完整池匹配、排序后分页。真实无结果为 total=0；超过最后一页保留真实总数、items 为空。来源不可用为 unavailable，保留错误分类、total 为空。 |
| stocks.meta、响应 meta | 股票范围、业务日、版本和过期状态来自当前 `mart.published_snapshot(product=screener)`；响应顶层另外返回 requestId。搜索读取当前版本。跨多个请求恰逢新发布时，调用方可比较每页版本。 |

优先级依次为代码完全匹配、名称完全匹配、代码或名称前缀、其他包含。同级按规范 symbol 升序。代码和名称匹配忽略大小写，百分号和下划线按字面处理。匹配只读发布池证券的代码与名称。刷新证券名称、代码或入池范围由现有证券主数据及选股发布任务负责。

## 网站导航与兼容

网站仓库 `src/modules/search/page-catalog.ts` 管理首页、市场、宏观、选股器四项的 id、标题、说明、关键词和相对路径。默认空关键词列出全部页面，其他关键词按标题完全匹配、标题或关键词前缀、包含排序，同级按 id。股票链接由网站从已校验的 symbol 生成。`src/modules/search/search-api.ts` 将两个结果在本站合并。即使上游意外返回导航路径，网站仍只使用本地目录与本地构造的工作台链接。

旧版合并响应继续含有 `data.pages`、证券项的 `href` 和 `meta.pageCatalogVersion`。证券来源返回 unavailable 时页面目录仍可匹配；参数无效或整次请求失败时保留原有错误响应。底座没有 InvestMind 页面目录，也不读取用户私有数据。

## 对账

在 2026-10-21 的 50 股发布池 `20261021-1c01ff6eb0b7d78a` 上，对新旧底座读路径分别通过实际回环 HTTP 查询。全角 `６００５１９.sh` 匹配 `600519.SH`，名称“银行”匹配 3 只，查询“6”匹配 29 只，两页各 2 个结果无重叠。每次非空合法请求都只执行 1 次当前发布指针读取，空关键词和 4 种无效请求均为 0 次 SQL。结果顺序逐项与完整发布池独立匹配的证券代码一致。[HTTP、SQL 次数与命中证券](research/investmind-security-search.json)

重放接口使用 `python -I scripts/audit-security-search.py --output docs/research/investmind-security-search.json`；在有当前生产构建的网站仓库加 `--site`，可同时核验 SSR 与两个旧网站路径。两者均使用本机临时回环服务及只读数据库。

网站生产构建的九次访问已通过。搜索页和两个兼容路径分别验证了证券“600519”、页面“宏观”和空输入，均发起一次 v1 搜索请求；非空各一条证券 SQL，空输入零条。SSR 搜索页另外读取一次根布局所需的组合快照，两个 JSON 兼容路由均没有布局读取。
