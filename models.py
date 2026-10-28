"""원본 글의 Category 모델."""
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import literal
from sqlalchemy.dialects import postgresql

db = SQLAlchemy()


def create_app(database_url=None):
    """DB 접속 주소를 설정한 Flask 앱을 만든다."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url or os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/tree"
    )
    db.init_app(app)
    return app


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True, nullable=False)
    # SQLAlchemy 는 foreign_key 를 dialect 옵션으로 보고 SAWarning 만 낸 뒤 무시한다. 그래서 DB 에 FK 제약이 없다.
    parent_id = db.Column(db.Integer, foreign_key=[id])
    name = db.Column(db.String)

    # FK 제약이 없으므로 조인 조건을 직접 적는다.
    parent = db.relationship(
        "Category",
        primaryjoin="Category.parent_id == Category.id",
        foreign_keys=[parent_id],
        remote_side=[id],
    )

    @classmethod
    def get(cls, id_):
        """특정 id 값의 데이터를 가져옵니다."""
        return cls.query.filter(cls.id == id_).one_or_none()

    def get_descendants(self):
        """category 인스턴스의 모든 자식(자손)을 가져와서 dictionary 형태로 보내줍니다."""
        children = Category.query.filter(Category.parent == self).all()
        children_list = []
        for child in children:
            ele = child.get_descendants()
            children_list.append(ele)
        children_list.sort(key=lambda ele: ele["id"])
        return {
            "id": self.id,
            "name": self.name,
            "children": children_list,
        }

    @classmethod
    def get_recursive_cte(cls):
        """루트부터 모든 노드의 경로(c_path)와 깊이(depth)를 구하는 재귀 CTE 를 만든다."""
        start_query = db.session.query(
            cls.id,
            cls.name,
            postgresql.array([cls.parent_id]).label("c_path"),
            literal(1).label("depth"),
        ).filter(
            cls.parent_id.is_(None)
        ).cte("category_cte", recursive=True)  # 재귀 쿼리임을 여기서 표현합니다

        repeat_query = db.session.query(
            cls.id,
            cls.name,
            start_query.c.c_path + postgresql.array([cls.parent_id]),
            start_query.c.depth + 1,
        ).filter(
            cls.parent_id == start_query.c.id
        )
        return start_query.union_all(repeat_query)

    @classmethod
    def get_result_recursive(cls, term):
        """재귀 쿼리 ORM을 이용해 데이터를 가져올 수 있습니다."""
        recursive_query = cls.get_recursive_cte()
        query = db.session.query(recursive_query)

        return query.filter(
            recursive_query.c.name.like(f"%{term}%")
        ).all()
