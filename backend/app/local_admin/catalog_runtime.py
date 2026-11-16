from app.local_admin.status import task_rows

def runtime_context(manager, table_loader):
    result = {"tasks": [], "tables": [], "tableState":"unknown", "issues":[]}
    try:
        result["tables"] = table_loader()
        result["tableState"] = "available"
    except Exception:
        result["issues"].append("数据库暂不可用，表是否存在待确认；文档仍可阅读。")
    if manager:
        try:
            result["tasks"] = task_rows(manager)
        except Exception:
            result["issues"].append("运行方回执暂不可用，调度生效状态待确认。")
    return result
