# 트리 쿼리 최적화 재현: 재귀 함수(AS-IS) vs 재귀 CTE(TO-BE)

예전에 Flask-SQLAlchemy로 카테고리 트리를 조회하면서 했던 최적화를 다시 재현한 프로젝트입니다.

- **AS-IS**: 노드마다 자식을 조회하는 재귀 함수 (`get_descendants`). 쿼리 수가 노드 수만큼 늘어나는 N+1 구조
- **TO-BE**: PostgreSQL 재귀 CTE로 트리 전체를 한 번에 조회 (`get_recursive_cte`)

당시 Flask-SQLAlchemy 코드를 바탕으로 두 방식을 구현했습니다. 실험 대상은 트리 조회 쿼리입니다. 두 방식의 결과가 같은지 `assert a == b`로 먼저 확인한 다음, 쿼리 수와 응답 시간을 비교합니다.

![DB 왕복 지연별 응답 시간](results/latency_chart.png)

카테고리 100개에서 DB 왕복 지연을 늘려 가며 잰 결과입니다. 점선은 블로그에 적어 둔 당시 실측값입니다.

## 결과 요약

- DB 왕복에 100 ms 지연을 넣으면 카테고리 100개에서 AS-IS 10.5초, TO-BE 206 ms로, 블로그에 적어 둔 당시 실측값(약 10초 → 약 200ms)과 거의 같습니다.
- 지연이 없는 로컬에서도 노드 1만 개에서 AS-IS는 **10,001번 쿼리에 약 15.7초**, TO-BE는 **1번 쿼리에 약 0.19초**가 걸렸습니다 (약 85배). 노드 수별 그래프는 [docs/experiment.md](docs/experiment.md)에 있습니다.

실험에는 `categories(id, parent_id, name)` 테이블 하나만 씁니다. 실험 설계, 모델 구조, 쿼리, 전체 결과와 해석은 [docs/experiment.md](docs/experiment.md)에 있습니다.

## 기술 스택

- PostgreSQL 16.4 (Docker)
- Python 3.12, Flask 3.0, Flask-SQLAlchemy 3.1.1, SQLAlchemy 2.0.36, psycopg 3.2
- 쿼리는 당시 코드 스타일에 맞춰 1.x 스타일 Query API(`Model.query`, `db.session.query(...)`)로 작성했습니다.
- toxiproxy 2.9.0 (DB 왕복 지연 조건)
- matplotlib (차트)

## 실행 방법

Docker만 있으면 됩니다. 명령 하나로 테이블 생성 → 데이터 생성 → A/B 결과 검증 → 측정 → `results/` 저장까지 진행됩니다. 기본 6개 조합과 DB 왕복 지연 조건을 차례로 측정하며, 4~5분 정도 걸립니다.

```bash
docker compose up --build --exit-code-from bench
```

검증만 빠르게 돌리려면(측정 없이 seed 검증과 `assert a == b`만 실행):

```bash
docker compose run --rm --build bench python bench.py --check
```

DB 왕복 지연 조건만 다시 재려면:

```bash
docker compose run --rm --build bench python bench.py --latency-only
```

끝난 뒤 컨테이너 정리:

```bash
docker compose down
```

### 직접 들여다보기

포트는 이 PC 안(`127.0.0.1`)에서만 열립니다.

| 서비스 | 호스트 주소 | 용도 |
| --- | --- | --- |
| db | `localhost:5432` (user/password `postgres`, DB `tree`) | psql, DB 툴, venv에서 직접 접속 |
| toxiproxy | `localhost:5433` | 지연이 걸리는 db 프록시 (bench가 프록시를 만든 뒤에만 열림) |
| toxiproxy | `localhost:8474` | 관리 API (예: `curl localhost:8474/proxies`) |

DB만 띄워 두기. 이 명령은 빈 DB만 띄우고, 테이블과 데이터는 만들지 않습니다. 벤치를 돌린 뒤 컨테이너가 멈춰만 있었다면 마지막으로 넣은 데이터(지연 조건의 노드 100개)가 그대로 남아 있습니다. `docker compose down`으로 컨테이너를 지웠다면 볼륨이 없어서 데이터도 함께 사라집니다.

```bash
docker compose up -d db toxiproxy
```

측정 없이 데이터만 넣기. `categories` 테이블을 다시 만들고(`schema.sql`) 노드 수만큼 트리를 넣은 뒤 seed 검증까지 합니다. 노드 수는 `100`, `1000`, `10000` 중 하나이고, 생략하면 `1000`입니다:

```bash
docker compose run --rm --build bench python seed.py 1000
```

db 컨테이너에서 psql 열기:

```bash
docker compose exec db psql -U postgres -d tree
```

bench 컨테이너 셸 열기. 프로젝트 폴더를 마운트해서, 호스트에서 고친 코드가 바로 반영됩니다:

```bash
docker compose run --rm -v "$PWD:/app" bench bash
```

venv에서 직접 돌리기. 접속 주소 기본값이 위 호스트 포트(`localhost:5432`, `5433`, `8474`)로 잡혀 있어서 환경변수 설정이 필요 없습니다. 단, DB 컨테이너는 먼저 띄워 둬야 합니다(`docker compose up -d db toxiproxy`). 또 호스트에 나눔고딕 폰트가 없으면 차트의 한글이 깨질 수 있습니다:

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python bench.py --check
```

venv에서 데이터만 넣으려면:

```bash
.venv/bin/python seed.py 1000
```

## 파일 구성

```
docker-compose.yml   # db(postgres:16.4) + toxiproxy(지연 조건) + bench
Dockerfile           # python:3.12-slim + 한글 폰트(차트용)
requirements.txt
schema.sql           # categories 테이블
models.py            # Category 모델 + get / get_descendants / get_result_recursive
seed.py              # 노드 수별 트리 생성, seed 검증 (단독 실행 시 데이터만 넣기)
methods.py           # A/B 트리 조회 함수 (Python 조립)
bench.py             # 검증 → 측정 → results/ 저장
docs/
└─ experiment.md     # 실험 설계, 모델 구조, 쿼리, 결과와 해석
results/
├─ results.csv / results.md / chart.png                # 기본 6개 조합
├─ latency.csv / latency.md / latency_chart.png       # DB 왕복 지연 조건
└─ explain/                                           # EXPLAIN ANALYZE 출력
```
