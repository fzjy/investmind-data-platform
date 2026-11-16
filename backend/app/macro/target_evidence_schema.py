"""独立证据结构/不可变保护只读门；不自动创建或修复。"""
import re

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text

from app.db.models.enterprise_target_evidence import TargetObservation, TargetCandidate

MODELS = (TargetObservation, TargetCandidate)
GUARD_BODY = "BEGIN RAISE EXCEPTION 'TARGET_EVIDENCE_IMMUTABLE'; END"


def _sql(value):
    """只解析本合同的等值/OR CHECK；保留字面量，未知语法失败关闭。

    PG的括号与text/无长度varchar格式可等价；不删除字面量内空白、
    大小写或括号，也不把AND、长度cast等语义变化归一化。
    """
    matches=list(re.finditer(r"\s+|'(?:[^']|'')*'|::|[A-Za-z_][A-Za-z_0-9]*|[=()]",value))
    if ''.join(m.group() for m in matches)!=value:return None
    tokens=[m.group() for m in matches if not m.group().isspace()];position=0

    def take(expected=None):
        nonlocal position
        if position>=len(tokens):raise ValueError
        token=tokens[position]
        if expected is not None and token.lower()!=expected:raise ValueError
        position+=1
        return token

    def peek(expected):
        return position<len(tokens) and tokens[position].lower()==expected

    def atom():
        token=take()
        if token=='(':
            node=expression();take(')')
        elif token.startswith("'"):
            node=('literal',token)
        elif re.fullmatch('[A-Za-z_][A-Za-z_0-9]*',token) and token.lower() not in ('or','and','not','true','false','null'):
            node=('identifier',token.lower())
        else:raise ValueError
        while peek('::'):
            if node[0] not in ('literal','identifier'):raise ValueError
            take('::');kind=take().lower()
            if kind=='character':take('varying')
            elif kind!='text':raise ValueError
        return node

    def comparison():
        node=atom()
        if peek('='):
            take('=');right=atom()
            if node[0]!='identifier' or right[0]!='literal':raise ValueError
            node=('eq',node,right)
        return node

    def expression():
        nodes=[comparison()]
        while peek('or'):
            take('or');nodes.append(comparison())
        if len(nodes)==1:return nodes[0]
        flattened=[]
        for node in nodes:
            if node[0]=='or':flattened.extend(node[1:])
            elif node[0]=='eq':flattened.append(node)
            else:raise ValueError
        return ('or',*flattened)

    try:
        result=expression()
        return result if position==len(tokens) and result[0] in ('eq','or') else None
    except (ValueError,RecursionError):return None


def target_evidence_schema(factory) -> tuple[str, ...]:
    return _evidence_schema(factory,MODELS,MODELS,'immutable_enterprise_target',
        'reject_enterprise_target_mutation',GUARD_BODY)


def _evidence_schema(factory,models,guarded_models,trigger_name,function_name,guard_body) -> tuple[str, ...]:
    problems = []
    with factory() as session:
        connection = session.connection()
        inspector = inspect(connection)
        for model in models:
            table = model.__table__; schema = table.schema
            if not inspector.has_table(table.name, schema=schema):
                problems.append(table.fullname + ':missing')
                continue
            columns = {c['name']: c for c in inspector.get_columns(table.name, schema=schema)}
            if set(columns) != set(table.c.keys()) or any(
                c.name not in columns or columns[c.name]['nullable'] != c.nullable
                or not isinstance(columns[c.name]['type'], type(c.type))
                or getattr(columns[c.name]['type'], 'length', None) != getattr(c.type, 'length', None)
                or (getattr(columns[c.name]['type'], 'timezone', None) != getattr(c.type, 'timezone', None))
                for c in table.c):
                problems.append(table.fullname + ':columns')
            if inspector.get_pk_constraint(table.name, schema=schema)['constrained_columns'] != [c.name for c in table.primary_key]:
                problems.append(table.fullname + ':primary-key')
            actual = {(tuple(f['constrained_columns']), f['referred_schema'], f['referred_table'], tuple(f['referred_columns']))
                for f in inspector.get_foreign_keys(table.name, schema=schema)}
            expected = {(tuple(e.parent.name for e in f.elements), f.elements[0].column.table.schema,
                f.elements[0].column.table.name, tuple(e.column.name for e in f.elements)) for f in table.foreign_key_constraints}
            if actual != expected: problems.append(table.fullname + ':foreign-key')
            actual_unique = {tuple(u['column_names']) for u in inspector.get_unique_constraints(table.name, schema=schema)}
            expected_unique = {tuple(c.name for c in u.columns) for u in table.constraints if isinstance(u, UniqueConstraint)}
            if actual_unique != expected_unique: problems.append(table.fullname + ':unique')
            actual_checks = {_sql(c['sqltext']) for c in inspector.get_check_constraints(table.name, schema=schema)}
            expected_checks = {_sql(str(c.sqltext)) for c in table.constraints if isinstance(c, CheckConstraint)}
            if None in actual_checks or None in expected_checks or actual_checks != expected_checks:
                problems.append(table.fullname + ':check')
        if not problems:
            rows = session.execute(text("""SELECT n.nspname || '.' || c.relname,p.prosrc
                FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
                JOIN pg_language l ON l.oid=p.prolang
                WHERE t.tgname=:trigger_name AND t.tgtype=27 AND t.tgqual IS NULL
                AND t.tgattr=''::int2vector
                AND t.tgnargs=0 AND NOT t.tgisinternal AND t.tgconstraint=0 AND t.tgenabled IN ('O','A')
                AND pn.nspname='ops' AND p.proname=:function_name AND p.pronargs=0
                AND NOT p.prosecdef AND p.prorettype='trigger'::regtype AND l.lanname='plpgsql'"""),
                {'trigger_name':trigger_name,'function_name':function_name})
            guarded = {name for name, body in rows if re.sub(r'\s+', ' ', body).strip() == guard_body}
            for model in guarded_models:
                if model.__table__.fullname not in guarded: problems.append(model.__table__.fullname + ':guard')
    return tuple(problems)
