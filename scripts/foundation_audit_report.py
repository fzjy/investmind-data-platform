"""把机器清单渲染成逐域、逐条目及逐凭证的中文索引。"""

from collections import Counter
import json
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "docs/foundation-audit"
DOMAINS = {
    "stocks": "证券与股票",
    "markets": "市场、指数、ETF 与商品",
    "macro": "宏观",
    "content": "财经内容及导入",
    "portfolio": "组合公共输入",
}


def read(name):
    return json.loads((DIR / name).read_text(encoding="utf8"))


def show_witness(w):
    if "rows" in w:
        return f"{w['rows']:,} 条" + ("指定条件记录" if w.get("query") else "主表记录")
    if "captures" in w:
        return f"{w['captures']:,} 条输入捕获"
    return "合同与发布证据"


def inventory_doc():
    inv, db = read("inventory.json"), read("database.json")
    lines = [
        "# 全域数据与消费者清单",
        "",
        f"本次核对五域目录中 {len(inv['items'])} 个条目，按实际注册路由核对归属。表中的公共路径省略共同前缀 `/api/v1/datasets/`。输入计数只证明留存凭证；计数为零的条目继续区分配置、上游缺口、字段缺失和产品无值。",
        "",
        "每项的来源、字段、任务、表、业务键、时间列、历史口径、API、直接和间接消费者、实库样本或查询位于机器清单。每个字段的单位、可空性、来源列和落表位置通过该项的 `fieldDetails` JSON Pointer 定位。仅有代码引用或对外路由，不足以证明某个页面实际调用。未登记的消费位置不推测。日期列以交易所业务日、报告期或事件日解释，数据库的带时区时间列以 UTC 记录。本机调度时区为 Asia/Shanghai。宏观观测日与发布时间、内容发布时间与采集时间各自独立。[逐项与入口](inventory.json) · [真实数据库对象与验证](database.json) · [供应商字段使用明细](../research/investmind-tushare-field-consumers.json) · [非 Tushare 字段](../research/investmind-non-tushare-field-usage.json)",
        "",
    ]
    for domain, title in DOMAINS.items():
        items = [x for x in inv["items"] if x["domain"] == domain]
        lines += [
            f"## {title}（{len(items)} 项）",
            "",
            "| 条目 | 归属与独立查询 | 本机取证 | 处置与范围 |",
            "| --- | --- | --- | --- |",
        ]
        for item in items:
            paths = "、".join("`" + p.split("/")[-1] + "`" for p in item["sharedRoutes"])
            disposition = item["destination"] + ("。" + paths if paths else "")
            lines.append(
                f"| {item['name']} `{item['id']}` | {disposition} | {show_witness(item['witness'])} | {item['decision']} |"
            )
        lines += [""]
    lines += [
        "## 数据库反查",
        "",
        "以下对象均来自同一只读快照。每张表的物理列、索引、约束、键检查、模型、引用它的源码、迁移及目录反向索引保存在 `database.json → relations`。源码引用为对全限定表名及 ORM 类名的反查，动态生成的 SQL 另看该项所列业务代码。行数按精确计数。关系未在目录中独列时，在下表标识用途。",
        "",
        "| 关系 | 记录 | 检查的键 | 目录关联 | 用途 |",
        "| --- | ---: | --- | ---: | --- |",
    ]
    for r in db["relations"]:
        lines.append(
            f"| `{r['rel']}` | {r['rows']:,} | `{', '.join(r['keyChecked'])}` | {len(r['catalogEntries'])} | {r['disposition']} |"
        )
    lines += [
        "",
        "## 作业和路由反查",
        "",
        f"代码注册了 {len(inv['cliCommands'])} 个 CLI 命令和 {len(inv['schedulerJobs'])} 个调度作业。逐个名称、实现模块和触发器位于 `inventory.json → cliCommands / schedulerJobs`。实际批次的来源、数据集、模式、结果以及用到的 API 见 `database.json → comparisons.rawTaskOwnership`，历史任务名、终态、时间范围及游标另列于 `actualRuns / cursorJobs`。CLI 注册与调度配置不能证明任务本机已运行。",
        "",
        "| 路由 | 方法 | 运行职责 |",
        "| --- | --- | --- |",
    ]
    for r in inv["routes"]:
        path = r["path"]
        role = (
            "独立领域事实"
            if path.startswith("/api/v1/datasets/")
            else (
                "兼容产品"
                if path.startswith("/api/data/")
                else (
                    "本机管理"
                    if path.startswith("/admin")
                    else (
                        "采集状态"
                        if "data-status" in path
                        else "产品及查询" if path.startswith("/api/v1") else "服务元数据"
                    )
                )
            )
        )
        lines.append(f"| `{path}` | {', '.join(r['methods']) or '挂载'} | {role} |")
    (DIR / "inventory.md").write_text("\n".join(lines) + "\n", encoding="utf8")


def dividend_doc():
    d = read("dividends.json")
    summary, events = d["summary"], d["events"]
    row_sources = summary["businessRowsSources"]
    by_id = {r["id"]: r for r in d["captures"]}
    changed = Counter(c["classification"] for group in d["revisedRequests"] for c in group["comparison"])
    lines = [
        "# 分红逐笔判定",
        "",
        "以来源、接口和请求哈希聚类，按捕获时刻、Raw ID 排序。组内每种正文第一次出现以后的相同捕获计一次额外捕获。判定旧原件是否可引用时，要求它在后一个批次开始前已完成。例行任务在 HTTP 返回后可能才创建批次，故仅有时间顺序不足以认定可复用。对已复用的记录还核对了冻结游标、具体旧原件、请求身份、两份正文、批次终态与实发计数。历史池回补按作业的证券、时间窗口、分页大小和偏移重新验算请求哈希。",
        "",
        f"{summary['captures']} 次捕获中 {summary['extraCaptures']} 次为额外捕获。已发生且记录为零请求的复用 {summary['alreadyReused']} 条，应重新获取 {summary['decisions'].get('应重新获取',0)} 条，证据不足 {summary['decisions'].get('证据不足',0)} 条。额外捕获所属批次的请求计数共 {summary['physicalRequestsRecordedForExtraCaptures']}，包含重试。本次对实际已发请求可证明的新增节省为 {summary['provenAdditionalSavings']}。{summary['revisedKeys']} 个同请求异文键与额外捕获的计数单位不同。[全部捕获、批次、游标及异文时间链](dividends.json)",
        "",
        f"组合专项的 {row_sources.get('portfolio_dividend_evidence',{}).get('http-bytes-decimal',0)} 份原件经 HTTP 状态、字节长度与 SHA-256、业务状态、分页、字段和规范块校验，从绑定原字节按 Decimal 解出小数。比较异文、计算业务行摘要、核对 Core 与判断补充均采用这组数值。每份原件保留 Raw ID 与行号，来源标识单独记录。常规捕获中 {row_sources.get('dividend',{}).get('stored-json-decimal',0)} 份缺少 HTTP 原字节，从 PostgreSQL 已持久化的 JSON 文本按 Decimal 读取；此前采集过程中丢失的供应商精度无法还原。每次捕获的 businessRowsSource 标明证据来源。",
        "",
        "异文按相邻两次捕获比较。"
        + "。".join(f"{k}共 {n} 次" for k, n in changed.items())
        + "。逐步新增、消失及取值变化的事件键见每组 comparison。日期与正文身份均保留，可据此回放相同请求在不同时间的实况。",
        "",
        f"按证券、报告期、公告日和阶段归组，{events['both']} 个 Core 键可在组合专项找到，专项另有 {events['specialtyOnly']:,} 个键。对两项现金和登记、除权、支付日逐原件变体比对，全部变体与 Core 一致的记录有 {events['coreVariantAgreement'].get('all',0)} 条，仅部分变体一致的有 {events['coreVariantAgreement'].get('some',0)} 条，全部不符的有 {events['coreVariantAgreement'].get('none',0)} 条。约定金额用十进制核对，缺失和数值零分开统计。",
        "",
        f"同一原件内有 {events['intraCaptureVariantKeys']} 个粗键对应多套字段。对在多份原件中出现的同键，比对完整变体集合，发生变化的有 {events['crossCaptureChangedKeys']} 个键。只要同键中出现过多个非空值、部分版本缺值，或有任一变体与 Core 可比字段不符，该补充字段就不计入确定值。符合确定补充口径的 Core 记录共 {len(events['enrichedCoreIds'])} 条，其中 {events['extraByField'].get('base_share',0)} 条获基准股本，{events['extraByField'].get('base_date',0)} 条获基准日。多腿事件、取消及其生效仍由事件合同裁决。[逐键全部变体和来源行](dividend-event-evidence.jsonl)",
        "",
        "来源明细按一行一个粗键保存。每个变体的 sources 列出 Raw ID 和从 1 开始的原件行号；matchedVariants 是当前键 variants 数组从 0 开始的下标。多捕获比较要求该键在两份原件中都出现。金额、日期规范化并保留字段是否存在，原件可用于核对原始表示。附加字段判定按字段独立进行。",
        "",
        "| Raw ID | 请求证券 | 批次目标日 | 前次同文 ID | 前次已完成 ID | 实际引用 ID | 本批实发 | 判定 | 原因 |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for key in d["extraRawIds"]:
        r = by_id[key]
        lines.append(
            f"| {key} | {r.get('params',{}).get('ts_code','未还原')} | {r.get('target') or '见回补窗口'} | {r['previousEqualRawId']} | {r.get('previousCompletedBeforeStart') or '—'} | {r.get('cachedSourcePayloadId') or '—'} | {r['request_count']} | {r['decision']} | {r['reason']} |"
        )
    (DIR / "dividends.md").write_text("\n".join(lines) + "\n", encoding="utf8")


if __name__ == "__main__":
    inventory_doc()
    dividend_doc()
