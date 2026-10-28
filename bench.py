"""엔트리포인트: 스키마 → seed → A/B assert → 측정 → results/ 저장.

    python bench.py                 # 전체 (6개 조합 + 네트워크 지연 조건)
    python bench.py --check         # seed 검증과 A == B assert 만
    python bench.py --latency-only  # 네트워크 지연 조건만
"""
import argparse
import csv
import json
import os
import platform
import statistics
import time
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

import methods  # noqa: E402
from models import create_app, db  # noqa: E402
from seed import run_schema, seed  # noqa: E402

SIZES = (100, 1_000, 10_000)
REPEAT = 5  # 워밍업 1회 뒤 측정 횟수 (중앙값 사용)
EXPLAIN_SIZES = (100, 10_000)  # 실행 계획을 저장할 노드 수
RESULTS = Path(__file__).parent / "results"

# 네트워크 지연 조건: 블로그 당시처럼 카테고리 100개에서 DB 왕복 지연만 늘려 본다
LATENCY_SIZE = 100
LATENCIES_MS = (0, 1, 5, 20, 50, 100)
TOXIPROXY_URL = os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
PROXY_DATABASE_URL = os.environ.get(
    "PROXY_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/tree"
)

METHODS = {"A": methods.tree_a, "B": methods.tree_b}


class QueryCounter:
    """before_cursor_execute 로 실행된 SQL 수를 집계한다. 마지막 실행의 SQL 도 기억한다."""

    def __init__(self, engine):
        """engine 에 before_cursor_execute 리스너를 등록한다."""
        self.count = 0
        self.statements = []
        event.listen(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        """실행되는 SQL 마다 수를 세고 SQL 과 파라미터를 기록한다."""
        self.count += 1
        self.statements.append((statement, parameters))

    def reset(self):
        """센 쿼리 수와 기록한 SQL 을 비운다."""
        self.count = 0
        self.statements = []


def analyze():
    """seed 직후 테이블 통계를 갱신해 실행 계획이 실제 데이터 기준으로 잡히게 한다."""
    db.session.execute(text("ANALYZE categories"))
    db.session.commit()


def run_once(fn, info, counter):
    """새 세션에서 fn 을 한 번 실행해 결과, 쿼리 수, 걸린 시간(ms)을 돌려준다."""
    # 새 세션이라 ORM identity map 이 이전 실행 결과를 재사용하지 않는다
    db.session.remove()
    counter.reset()
    started = time.perf_counter()
    result = fn(info.root_id)
    elapsed_ms = (time.perf_counter() - started) * 1000
    queries = counter.count
    db.session.remove()
    return result, queries, elapsed_ms


# --- 정확성 검증 -----------------------------------------------------------

def collect_ids(node, out):
    """트리의 모든 노드 id 를 out 에 모은다."""
    out.append(node["id"])
    for child in node["children"]:
        collect_ids(child, out)
    return out


def check(a, b, info):
    """A 와 B 결과가 같고 seed 의 모든 노드를 한 번씩 담았는지 assert 하고 노드 수를 돌려준다."""
    assert a == b, "A 와 B 결과가 다르다"
    ids = collect_ids(a, [])
    assert len(ids) == len(set(ids)), "같은 노드가 두 번 나온다"
    assert set(ids) == set(info.parent)
    return len(ids)


# --- EXPLAIN ANALYZE -------------------------------------------------------

def explain(statements, name, label):
    """실제로 실행된 SQL 과 파라미터를 그대로 EXPLAIN ANALYZE 해서 results/explain 에 저장한다."""
    out = RESULTS / "explain"
    out.mkdir(parents=True, exist_ok=True)
    statements = list(statements)  # EXPLAIN 실행도 counter.statements 에 쌓이므로 복사본을 돈다
    for i, (statement, parameters) in enumerate(statements, 1):
        suffix = f"_{i}" if len(statements) > 1 else ""
        plan = db.session.connection().exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS) " + statement, parameters
        ).scalars().all()
        db.session.rollback()
        body = f"-- {label}\n-- parameters: {parameters}\n{statement}\n\n" + "\n".join(plan) + "\n"
        (out / f"{name}{suffix}.txt").write_text(body)


def save_explains(info, counter):
    """A 자식 조회와 B 재귀 CTE 쿼리의 실행 계획을 저장한다."""
    tag = f"n{info.n}"
    _, _, _ = run_once(methods.tree_a, info, counter)
    # A 의 두 번째 쿼리 = 루트의 자식 조회 (첫 번째는 Category.get)
    explain([counter.statements[1]], f"{tag}_A_children", "A: 노드 하나의 자식 조회 (루트)")
    run_once(methods.tree_b, info, counter)
    explain(counter.statements, f"{tag}_B_tree_cte", "B: 재귀 CTE")


# --- 결과 저장 -------------------------------------------------------------

def save_csv(rows, filename):
    """측정 행을 results/ 아래 CSV 로 저장한다."""
    with open(RESULTS / filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def save_markdown(rows, pg_version):
    """기본 6개 조합 결과를 results/results.md 표로 저장한다."""
    lookup = {(r["nodes"], r["method"]): r for r in rows}
    lines = [
        "# 결과",
        "",
        f"- PostgreSQL: {pg_version}",
        f"- Python {platform.python_version()}, SQLAlchemy {version('SQLAlchemy')}, "
        f"Flask-SQLAlchemy {version('Flask-SQLAlchemy')}, psycopg {version('psycopg')}",
        f"- 측정: 워밍업 1회 후 {REPEAT}회 실행의 중앙값, 실행마다 새 세션",
        "- 환경: 로컬 Docker (bench 컨테이너 → db 컨테이너), 네트워크 지연 추가 없음, parent_id 인덱스 없음",
        "",
        "| 노드 수 | A 쿼리 수 | B 쿼리 수 | A (ms) | B (ms) | A / B |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for n in SIZES:
        a = lookup[(n, "A")]
        b = lookup[(n, "B")]
        lines.append(
            f"| {n:,} | {a['queries']:,} | {b['queries']:,} "
            f"| {a['median_ms']:,.1f} | {b['median_ms']:,.1f} | {a['median_ms'] / b['median_ms']:,.1f}x |"
        )
    (RESULTS / "results.md").write_text("\n".join(lines) + "\n")


def save_chart(rows):
    """노드 수별 응답 시간 차트를 results/chart.png 로 저장한다."""
    # 색 = 방식(A/B). 두 색은 dataviz 기본 팔레트 1·2번 (검증 통과)
    colors = {"A": "#eb6834", "B": "#2a78d6"}
    plt.rcParams["font.family"] = ["NanumGothic", "DejaVu Sans"]
    fig, ax = plt.subplots(figsize=(7, 4.6), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    for method in ("A", "B"):
        points = sorted((r["nodes"], r["median_ms"], r["queries"]) for r in rows if r["method"] == method)
        xs, ys, queries = zip(*points)
        label = "A 재귀 함수" if method == "A" else "B 재귀 CTE"
        ax.plot(xs, ys, "-", color=colors[method], linewidth=2, marker="o",
                markersize=7, markeredgecolor="#fcfcfb", markeredgewidth=1.5, label=label)
        ax.annotate(f"{queries[-1]:,} 쿼리 · {ys[-1]:,.0f} ms", (xs[-1], ys[-1]), xytext=(8, 0),
                    textcoords="offset points", va="center", fontsize=8.5, color="#52514e")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks(SIZES, [f"{n:,}" for n in SIZES])
    ax.set_xlim(70, 30_000)
    ax.set_title("노드 수별 트리 조회 응답 시간", loc="left", fontsize=11, color="#0b0b0b")
    ax.set_xlabel("노드 수 (로그)", color="#52514e")
    ax.set_ylabel("응답 시간 중앙값 ms (로그)", color="#52514e")
    ax.grid(True, which="major", color="#e1e0d9", linewidth=0.8)
    ax.tick_params(colors="#898781", labelcolor="#52514e")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")
    ax.legend(frameon=False, fontsize=8, labelcolor="#52514e", loc="upper left", handlelength=4)
    fig.tight_layout()
    fig.savefig(RESULTS / "chart.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


# --- 측정 ------------------------------------------------------------------

def measure_block(info, counter, label, check_only=False):
    """한 조건(노드 수·지연)에서 A/B 를 검증하고 측정한다."""
    # 워밍업 겸 정확성 검증
    (a, a_queries, _), (b, b_queries, _) = (run_once(METHODS[m], info, counter) for m in "AB")
    nodes = check(a, b, info)
    print(f"[check] {label}: A == B ({nodes} nodes), queries A={a_queries} B={b_queries}")
    if check_only:
        return []
    combos = {"A": {"queries": a_queries, "times": []}, "B": {"queries": b_queries, "times": []}}
    # A/B 를 라운드마다 번갈아 실행해서 일시적인 호스트 잡음이 한쪽에 몰리지 않게 한다
    for _ in range(REPEAT):
        for method, combo in combos.items():
            _, run_queries, elapsed_ms = run_once(METHODS[method], info, counter)
            assert run_queries == combo["queries"]
            combo["times"].append(elapsed_ms)
    rows = []
    for method, combo in combos.items():
        times = combo["times"]
        rows.append({
            "method": method, "queries": combo["queries"],
            "median_ms": round(statistics.median(times), 3),
            "min_ms": round(min(times), 3), "max_ms": round(max(times), 3),
        })
        print(f"[bench] {label} {method}: "
              f"median {statistics.median(times):,.1f} ms ({combo['queries']} queries)")
    return rows


# --- 네트워크 지연 조건 (toxiproxy) ----------------------------------------
# bench → toxiproxy → db 로 접속하고, db 의 응답에 지연을 넣어 DB 왕복 1회마다 그만큼 늦어지게 한다.

def toxiproxy(method, path, body=None):
    """toxiproxy 관리 API 를 호출한다."""
    request = urllib.request.Request(
        TOXIPROXY_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code != 404:  # 지우려는 proxy/toxic 이 없을 때는 무시
            raise


def setup_proxy():
    """toxiproxy 가 뜰 때까지 기다린 뒤 db 로 가는 프록시를 새로 만든다."""
    for _ in range(30):
        try:
            toxiproxy("GET", "/version")
            break
        except urllib.error.URLError:
            time.sleep(1)
    toxiproxy("DELETE", "/proxies/postgres")
    toxiproxy("POST", "/proxies", {
        "name": "postgres", "listen": "0.0.0.0:5433", "upstream": "db:5432", "enabled": True,
    })


def set_latency(latency_ms):
    """db 응답마다 latency_ms 만큼 지연을 넣는다 (0 이면 지연 제거)."""
    toxiproxy("DELETE", "/proxies/postgres/toxics/latency")
    if latency_ms:
        toxiproxy("POST", "/proxies/postgres/toxics", {
            "name": "latency", "type": "latency", "stream": "downstream", "toxicity": 1.0,
            "attributes": {"latency": latency_ms, "jitter": 0},
        })


def measure_roundtrip():
    """실제로 걸리는 DB 왕복 시간: SELECT 1 의 중앙값."""
    times = []
    with db.engine.connect() as conn:
        for _ in range(10):
            started = time.perf_counter()
            conn.exec_driver_sql("SELECT 1").scalar()
            times.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(times), 3)


def run_latency_sweep(check_only):
    """블로그 당시 조건(카테고리 100개)에서 DB 왕복 지연을 늘려 가며 A/B 를 잰다."""
    setup_proxy()
    info = seed(LATENCY_SIZE)
    analyze()
    rows = []
    proxy_app = create_app(PROXY_DATABASE_URL)
    with proxy_app.app_context():
        counter = QueryCounter(db.engine)
        for latency_ms in LATENCIES_MS:
            set_latency(latency_ms)
            rtt_ms = measure_roundtrip()
            label = f"n={info.n} latency=+{latency_ms}ms (SELECT 1 왕복 {rtt_ms:.1f} ms)"
            for row in measure_block(info, counter, label, check_only):
                rows.append({"nodes": info.n, "latency_ms": latency_ms, "rtt_ms": rtt_ms, **row})
        set_latency(0)
        db.session.remove()
    return rows


def save_latency_markdown(rows):
    """지연 조건 결과를 results/latency.md 표로 저장한다."""
    lookup = {(r["latency_ms"], r["method"]): r for r in rows}
    lines = [
        "# 결과: DB 왕복 지연 조건",
        "",
        f"- 노드 {LATENCY_SIZE}개, parent_id 인덱스 없음, 워밍업 1회 후 {REPEAT}회 실행의 중앙값",
        "- bench → toxiproxy → db. toxiproxy 가 db 응답(downstream)마다 '추가 지연' 만큼 늦게 전달한다",
        "- 'SELECT 1 왕복'은 그 조건에서 실제로 잰 DB 왕복 1회 시간",
        "",
        "| 추가 지연 | SELECT 1 왕복 (ms) | A 쿼리 수 | B 쿼리 수 | A (ms) | B (ms) | A / B |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for latency_ms in LATENCIES_MS:
        a = lookup[(latency_ms, "A")]
        b = lookup[(latency_ms, "B")]
        lines.append(
            f"| +{latency_ms} ms | {a['rtt_ms']:,.1f} | {a['queries']:,} | {b['queries']:,} "
            f"| {a['median_ms']:,.1f} | {b['median_ms']:,.1f} | {a['median_ms'] / b['median_ms']:,.1f}x |"
        )
    (RESULTS / "latency.md").write_text("\n".join(lines) + "\n")


def save_latency_chart(rows):
    """지연 조건별 응답 시간 차트를 results/latency_chart.png 로 저장한다."""
    colors = {"A": "#eb6834", "B": "#2a78d6"}
    plt.rcParams["font.family"] = ["NanumGothic", "DejaVu Sans"]
    fig, ax = plt.subplots(figsize=(7, 4.6), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    # 블로그에 적어 둔 당시 실측값 (API 전체 응답 시간)
    for value, text_ in ((10_000, "당시 실측 AS-IS ≈ 10 s"), (200, "당시 실측 TO-BE ≈ 200 ms")):
        ax.axhline(value, color="#898781", linewidth=1, linestyle=":")
        ax.annotate(text_, (2.5, value), xytext=(0, 4), textcoords="offset points",
                    ha="center", fontsize=8, color="#898781")
    positions = range(len(LATENCIES_MS))  # 지연 조건을 같은 간격으로 놓는다
    for method in ("A", "B"):
        points = sorted((r["latency_ms"], r["median_ms"], r["queries"]) for r in rows if r["method"] == method)
        _, ys, queries = zip(*points)
        label = "A 재귀 함수" if method == "A" else "B 재귀 CTE"
        ax.plot(positions, ys, "-", color=colors[method], linewidth=2, marker="o", markersize=7,
                markeredgecolor="#fcfcfb", markeredgewidth=1.5, label=f"{label} ({queries[0]:,} 쿼리)")
        ax.annotate(f"{ys[-1]:,.0f} ms", (positions[-1], ys[-1]), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=8.5, color="#52514e")
    ax.set_yscale("log")
    ax.set_xticks(positions, [f"+{ms}" for ms in LATENCIES_MS])
    ax.set_xlim(-0.4, len(LATENCIES_MS) - 0.6)
    ax.set_title(f"DB 왕복 지연별 트리 조회 응답 시간 · 노드 {LATENCY_SIZE}개", loc="left", fontsize=11, color="#0b0b0b")
    ax.set_xlabel("DB 왕복 1회당 추가 지연 (ms)", color="#52514e")
    ax.set_ylabel("응답 시간 중앙값 ms (로그)", color="#52514e")
    ax.grid(True, which="major", color="#e1e0d9", linewidth=0.8)
    ax.tick_params(colors="#898781", labelcolor="#52514e")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")
    ax.legend(frameon=False, fontsize=8, labelcolor="#52514e", loc="lower right", handlelength=4)
    fig.tight_layout()
    fig.savefig(RESULTS / "latency_chart.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


# --- main ------------------------------------------------------------------

def main():
    """스키마 생성부터 측정, 결과 저장까지 전체 벤치를 실행한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="seed 검증과 A == B assert 만 실행")
    parser.add_argument("--latency-only", action="store_true", help="네트워크 지연 조건만 실행")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        counter = QueryCounter(db.engine)
        run_schema()
        pg_version = db.session.execute(text("SHOW server_version")).scalar()

        if not args.latency_only:
            rows = []
            for n in SIZES:
                info = seed(n)
                analyze()
                for row in measure_block(info, counter, f"n={n}", args.check):
                    rows.append({"nodes": n, **row})
                if n in EXPLAIN_SIZES and not args.check:
                    save_explains(info, counter)
            if not args.check:
                save_csv(rows, "results.csv")
                save_markdown(rows, pg_version)
                save_chart(rows)
                print(f"[done] {len(rows)}개 조합 → {RESULTS}")

        latency_rows = run_latency_sweep(args.check)
        if not args.check:
            save_csv(latency_rows, "latency.csv")
            save_latency_markdown(latency_rows)
            save_latency_chart(latency_rows)
            print(f"[done] 지연 조건 {len(latency_rows)}개 → {RESULTS}")


if __name__ == "__main__":
    main()
