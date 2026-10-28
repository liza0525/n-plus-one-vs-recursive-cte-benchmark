"""A(AS-IS) / B(TO-BE) 트리 조회 함수.

쿼리는 models.Category 의 메서드가 만들고, 여기서는 트리 형태로 맞추기만 한다.
출력은 모두 {"id", "name", "children": [...]} 이고 children 은 id 순이다.
"""
from models import Category, db


def tree_a(root_id):
    """AS-IS: 루트부터 노드마다 자식을 조회해 트리를 만든다 (1 + N 쿼리)."""
    return Category.get(root_id).get_descendants()


def tree_b(root_id):
    """TO-BE: 재귀 CTE 한 번으로 전체를 가져와 Python 에서 조립한다 (1 쿼리, root_id 는 쓰지 않음)."""
    rows = db.session.query(Category.get_recursive_cte()).all()
    return assemble(rows)


def assemble(rows):
    """CTE 행(id, name, c_path, depth)을 c_path 마지막 원소(부모 id) 기준으로 트리로 조립한다."""
    nodes = {row.id: {"id": row.id, "name": row.name, "children": []} for row in rows}
    root = None
    for row in sorted(rows, key=lambda row: row.id):
        parent_id = row.c_path[-1]
        if parent_id is None:
            root = nodes[row.id]
        else:
            nodes[parent_id]["children"].append(nodes[row.id])
    return root
