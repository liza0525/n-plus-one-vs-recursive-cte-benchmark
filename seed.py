"""노드 수별 트리 생성.

- seed 고정: 같은 노드 수면 매번 같은 트리
- 루트 1개, 자식이 있는 노드는 자식 수 1~10, 최대 깊이(루트=1)는 노드 수에 따라 4~6

    python seed.py         # 테이블을 다시 만들고 노드 1,000개 트리를 넣는다
    python seed.py 10000   # 노드 수 지정 (100 / 1000 / 10000)
"""
import argparse
import hashlib
import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from models import Category, create_app, db

SEED = 42
MAX_CHILDREN = 10
# 노드 수가 커질수록 깊게. 깊이 d 까지 담을 수 있는 최대 노드 수(1+10+...+10^(d-1))가 N 보다 크다.
MAX_DEPTH = {100: 4, 1_000: 5, 10_000: 6}


@dataclass
class TreeInfo:
    n: int
    root_id: int
    parent: dict  # id -> parent id (루트는 None)
    depth: dict  # id -> 깊이 (루트 = 1)

    def name(self, id_):
        """노드 이름 node-{id} 를 돌려준다."""
        return f"node-{id_}"


def generate_tree(n, seed=SEED):
    """seed 고정 난수로 노드 n 개 트리(id -> 부모 id)를 만든다."""
    # 자식 자리가 남은 노드 중 하나를 골라 붙이고, id 는 BFS 순서로 다시 매긴다
    rng = random.Random(seed * 100_000 + n)
    max_depth = MAX_DEPTH.get(n, 6)

    parent, depth, child_count = [None], [1], [0]
    eligible = [0]  # 자식을 더 붙일 수 있는 노드
    while len(parent) < n:
        i = rng.randrange(len(eligible))
        p = eligible[i]
        node = len(parent)
        parent.append(p)
        depth.append(depth[p] + 1)
        child_count.append(0)
        child_count[p] += 1
        if child_count[p] == MAX_CHILDREN:
            eligible[i] = eligible[-1]
            eligible.pop()
        if depth[node] < max_depth:
            eligible.append(node)

    children = [[] for _ in range(n)]
    for node in range(1, n):
        children[parent[node]].append(node)

    new_id = {}
    queue = deque([0])
    while queue:
        node = queue.popleft()
        new_id[node] = len(new_id) + 1
        queue.extend(children[node])

    return {
        new_id[node]: (new_id[parent[node]] if parent[node] is not None else None)
        for node in range(n)
    }


def build(n, seed=SEED):
    """트리를 만들고 깊이를 계산해 TreeInfo 로 돌려준다."""
    parent = generate_tree(n, seed)
    depth = {}
    for id_ in sorted(parent):  # BFS 순서라 부모가 항상 먼저 나온다
        depth[id_] = 1 if parent[id_] is None else depth[parent[id_]] + 1
    root_id = next(id_ for id_, p in parent.items() if p is None)
    return TreeInfo(n=n, root_id=root_id, parent=parent, depth=depth)


def tree_hash(info):
    """트리의 (id, 부모, 이름) 전체를 sha256 으로 해시한다."""
    rows = "\n".join(f"{id_},{info.parent[id_]},{info.name(id_)}" for id_ in sorted(info.parent))
    return hashlib.sha256(rows.encode()).hexdigest()


def run_schema():
    """schema.sql 을 실행해 categories 테이블을 다시 만든다."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    for statement in sql.split(";"):
        body = "\n".join(line for line in statement.splitlines() if not line.strip().startswith("--"))
        if body.strip():
            db.session.execute(text(body))
    db.session.commit()


def seed(n):
    """categories 를 비우고 노드 n 개 트리를 넣은 뒤 검증한다."""
    info = build(n)
    assert tree_hash(info) == tree_hash(build(n)), "같은 seed 인데 트리가 다르다"

    db.session.execute(text("TRUNCATE categories RESTART IDENTITY"))
    db.session.execute(
        Category.__table__.insert(),
        [{"id": id_, "parent_id": info.parent[id_], "name": info.name(id_)} for id_ in sorted(info.parent)],
    )
    db.session.execute(text("SELECT setval('categories_id_seq', (SELECT max(id) FROM categories))"))
    db.session.commit()

    verify(info)
    return info


def verify(info):
    """DB 에 들어간 트리가 seed 규칙(행 수, 루트, 깊이, 자식 수)을 지키는지 assert 한다."""
    child_counts = {}
    for p in info.parent.values():
        if p is not None:
            child_counts[p] = child_counts.get(p, 0) + 1
    max_depth = max(info.depth.values())

    db_count = db.session.execute(text("SELECT count(*) FROM categories")).scalar()
    db_roots = db.session.execute(text("SELECT count(*) FROM categories WHERE parent_id IS NULL")).scalar()

    print(
        f"[seed] n={info.n} db_rows={db_count} roots={db_roots} max_depth={max_depth} "
        f"children={min(child_counts.values())}~{max(child_counts.values())} "
        f"hash={tree_hash(info)[:12]}"
    )
    assert db_count == info.n
    assert db_roots == 1
    assert 4 <= max_depth <= 6
    assert 1 <= min(child_counts.values()) and max(child_counts.values()) <= MAX_CHILDREN


def main():
    """명령행에서 노드 수를 받아 테이블을 다시 만들고 데이터만 넣는다."""
    parser = argparse.ArgumentParser(description="categories 테이블을 다시 만들고 트리 데이터만 넣는다")
    parser.add_argument("n", nargs="?", type=int, default=1_000, choices=sorted(MAX_DEPTH), help="노드 수")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        run_schema()
        seed(args.n)


if __name__ == "__main__":
    main()
