"""执行器的完整只读结构门，不自动迁移/创建。"""
from app.db.models.enterprise_target_execution import TargetExecutionPlan, TargetExecutionAttempt
from app.macro.target_evidence_schema import target_evidence_schema, _evidence_schema


def target_execution_schema(factory) -> tuple[str,...]:
    problems=target_evidence_schema(factory)
    return problems+_evidence_schema(factory,(TargetExecutionPlan,TargetExecutionAttempt),(TargetExecutionPlan,),
        'immutable_enterprise_target_execution','reject_enterprise_target_execution_mutation',
        "BEGIN RAISE EXCEPTION 'TARGET_EXECUTION_IMMUTABLE'; END")
