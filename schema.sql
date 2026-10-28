-- parent_id 에는 FK 제약을 두지 않는다
-- (모델의 foreign_key=[id] 는 SQLAlchemy 가 무시하는 인자라 제약이 생기지 않음).
-- parent_id 에는 인덱스를 두지 않는다.
DROP TABLE IF EXISTS categories;

CREATE TABLE categories (
    id        SERIAL PRIMARY KEY,
    parent_id INTEGER,
    name      VARCHAR
);
