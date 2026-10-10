"""资料自助入库：上传 → 后台解析 → 增量建索引 → 按内容去重。

压三类风险：

1. **重复语料压排名**。`docs/23` §3 查明 net 检索只有 70% 的根因是同一段内容
   进了两次（合订本 + 按章课件），命中的是"对的内容、错的出处"。去重不生效
   不会报错，只会让引用指错地方。
2. **失败被吞掉**。扫描件解析出 0 块、文件损坏、后缀不支持 —— 这些如果只是
   "不出现在列表里"，教务会以为传丢了而反复重传，真正的原因一次都没被看见。
3. **同名覆盖**。改了一版又传一次，旧版分块还在索引里，两版会同时被检索到
   且标着同一个出处。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import courses as courses_mod
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token
from backend.rag import indexer, retriever
from backend.rag.chunking import Chunk


@pytest.fixture()
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "raw_dir", str(tmp_path / "raw"))
    monkeypatch.setattr(settings, "withdrawn_dir", str(tmp_path / "withdrawn"))
    monkeypatch.setattr(settings, "rag_fts_path", str(tmp_path / "fts.db"))
    monkeypatch.setattr(settings, "rag_mode", "fts5")
    # 没有视觉端点 = 用例默认不会走 OCR 兜底。不钉死这一条的话，一份"能打开但
    # 没有文本层"的 PDF 会让上传用例**真的去调视觉模型**：跑一次测试花一次钱，
    # 而且离线跑就红。要测 OCR 的用例自己把它打开（见文件末尾那三条）。
    monkeypatch.setattr(settings, "llm_vision_model", "")
    courses_mod.invalidate_cache()
    retriever.reset_engines()
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE username='demo_ta'")
    yield TestClient(app), issue_token("demo_ta", "admin"), tmp_path
    courses_mod.invalidate_cache()
    retriever.reset_engines()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _chunk(text: str, course: str = "net", source: str = "某教材", idx: int = 0,
           doc_type: str = "courseware") -> Chunk:
    return Chunk(text=text, source=source, doc_type=doc_type, course=course,
                 page_start=1, page_end=1, chunk_index=idx)


# --------------------------------------------------------------------------
# 1. 去重（本轮的主要目的）
# --------------------------------------------------------------------------
def test_identical_content_is_indexed_once(tmp_path):
    """同一段内容第二次写入被跳过 —— 这正是 net 检索 70% 的根因。"""
    conn = indexer.connect(tmp_path / "fts.db")
    first = indexer.write_chunks(conn, [_chunk("TCP 三次握手用于建立连接")])
    second = indexer.write_chunks(conn, [_chunk("TCP 三次握手用于建立连接", source="另一本合订本")])
    assert first == {"written": 1, "duplicate": 0}
    assert second == {"written": 0, "duplicate": 1}, "同内容不同出处仍应算重复"
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    conn.close()


def test_duplicates_within_one_batch_are_caught(tmp_path):
    """一份文件内部的重复页也要挡 —— 它们在同一批里，光查库查不到。"""
    conn = indexer.connect(tmp_path / "fts.db")
    stat = indexer.write_chunks(conn, [_chunk("同一页", idx=i) for i in range(3)])
    assert stat == {"written": 1, "duplicate": 2}
    conn.close()


def test_whitespace_only_differences_are_duplicates(tmp_path):
    conn = indexer.connect(tmp_path / "fts.db")
    indexer.write_chunks(conn, [_chunk("滑动窗口   协议\n\n用于流量控制")])
    stat = indexer.write_chunks(conn, [_chunk("滑动窗口 协议 用于流量控制")])
    assert stat["duplicate"] == 1
    conn.close()


def test_same_text_in_different_courses_is_not_a_duplicate(tmp_path):
    """查询本来就按 course 过滤，跨课程的同文不会互相压排名，不该去重。"""
    conn = indexer.connect(tmp_path / "fts.db")
    indexer.write_chunks(conn, [_chunk("缓存命中率的定义", course="net")])
    stat = indexer.write_chunks(conn, [_chunk("缓存命中率的定义", course="co")])
    assert stat["written"] == 1
    conn.close()


def test_backfill_lets_an_old_index_dedupe(tmp_path):
    """去重之前建的索引库（有 chunks 没 chunk_hashes）第一次遇上新代码。

    不回填的话，已经在库里的内容会被当成新内容再写一遍 —— 去重上线的
    第一次增量入库反而把重复翻倍。
    """
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(indexer.FTS_DDL)
    raw.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)",
                ("旧内容", "net", "courseware", "老教材", "", 1, 1, 0))
    raw.commit()
    raw.close()

    conn = indexer.connect(path)
    assert indexer.backfill_hashes(conn) == 1
    assert indexer.backfill_hashes(conn) == 0, "回填只该做一次"
    stat = indexer.write_chunks(conn, [_chunk("旧内容")])
    assert stat["duplicate"] == 1, "回填之后才认得出老内容"
    conn.close()


# --------------------------------------------------------------------------
# 2. 上传接口
# --------------------------------------------------------------------------
def _upload(client, token, name: str, payload: bytes, course="net", doc_type="courseware"):
    return client.post("/admin/materials", headers=_auth(token),
                       files={"file": (name, payload, "application/octet-stream")},
                       data={"course_id": course, "doc_type": doc_type})


def test_upload_stores_file_and_creates_job(admin_client):
    client, token, tmp_path = admin_client
    r = _upload(client, token, "第3章 数据链路层.pdf", b"%PDF-1.4 fake")
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["course_id"] == "net" and job["doc_type"] == "courseware"
    assert job["uploaded_by"] == "demo_ta"
    assert (tmp_path / "raw" / "net" / "courseware" / "第3章 数据链路层.pdf").is_file()


def test_upload_rejects_unknown_course_and_doc_type(admin_client):
    client, token, _ = admin_client
    assert _upload(client, token, "a.pdf", b"x", course="nope").status_code == 400
    assert _upload(client, token, "a.pdf", b"x", doc_type="past_papers").status_code == 400


def test_upload_rejects_unsupported_extension(admin_client):
    client, token, _ = admin_client
    r = _upload(client, token, "扫描件.jpg", b"\xff\xd8\xff")
    assert r.status_code == 400
    assert "OCR" in r.json()["detail"], "要说清该怎么办，而不是只说不支持"


def test_upload_rejects_empty_file(admin_client):
    client, token, _ = admin_client
    assert _upload(client, token, "empty.pdf", b"").status_code == 400


def test_upload_refuses_to_overwrite_same_name(admin_client):
    """同名不静默覆盖：旧版分块还在索引里，覆盖后两版会同时被检索到。"""
    client, token, _ = admin_client
    assert _upload(client, token, "讲义.pdf", b"%PDF v1").status_code == 200
    r = _upload(client, token, "讲义.pdf", b"%PDF v2")
    assert r.status_code == 409 and "同名" in r.json()["detail"]


def test_filename_cannot_escape_the_corpus_directory(admin_client):
    """文件名直接进路径，必须先剥掉目录成分。"""
    client, token, tmp_path = admin_client
    r = _upload(client, token, "../../../../etc/passwd.pdf", b"%PDF")
    assert r.status_code == 200, r.text
    # 服务器路径不再下发（交付产品不暴露目录结构），直接看文件落在哪
    assert "stored_path" not in r.json()["job"]
    assert (tmp_path / "raw" / "net" / "courseware" / "passwd.pdf").is_file()


def test_upload_is_admin_only(admin_client):
    client, _, _ = admin_client
    for role in ("student", "ta", "teacher"):
        r = client.post("/admin/materials", headers=_auth(issue_token("x", role)),
                        files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                        data={"course_id": "net", "doc_type": "courseware"})
        assert r.status_code == 403


# --------------------------------------------------------------------------
# 3. 后台任务与失败留痕
# --------------------------------------------------------------------------
def test_unparseable_file_is_marked_failed_with_a_reason(admin_client):
    """假 PDF 解析不出内容 —— 必须落成 failed 并写明原因，而不是悄悄消失。"""
    client, token, _ = admin_client
    job_id = _upload(client, token, "坏文件.pdf", b"not really a pdf").json()["job"]["id"]
    # TestClient 会在响应返回前跑完 BackgroundTasks
    job = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert job["status"] == "failed"
    assert job["error"], "失败必须带原因"


def test_job_list_reports_status_breakdown(admin_client):
    client, token, _ = admin_client
    _upload(client, token, "坏1.pdf", b"junk")
    _upload(client, token, "坏2.pdf", b"junk")
    body = client.get("/admin/materials", headers=_auth(token)).json()
    assert len(body["items"]) == 2
    assert body["by_status"].get("failed") == 2
    assert body["items"][0]["id"] > body["items"][1]["id"], "新的排前面"


def test_job_list_can_filter_by_course(admin_client):
    client, token, _ = admin_client
    _upload(client, token, "n.pdf", b"junk", course="net")
    _upload(client, token, "o.pdf", b"junk", course="os")
    items = client.get("/admin/materials?course_id=os", headers=_auth(token)).json()["items"]
    assert len(items) == 1 and items[0]["course_id"] == "os"


def test_unknown_job_is_404(admin_client):
    client, token, _ = admin_client
    assert client.get("/admin/materials/99999", headers=_auth(token)).status_code == 404


def test_successful_ingest_records_dedup_counts(admin_client, monkeypatch):
    """走完整条上传链路，确认解析/入库/去重三个计数都如实回填。

    用一份假的 parse_one 而不是造真 PDF：这条用例压的是接口与任务的接线，
    PyMuPDF 的解析能力由 chunking 那组用例负责。
    """
    client, token, _ = admin_client
    from backend.rag import ingest as ingest_mod

    payload = [_chunk("重复的一段", idx=0), _chunk("重复的一段", idx=1),
               _chunk("独一份的一段", idx=2)]
    monkeypatch.setattr(ingest_mod, "parse_one", lambda item, raw_dir: payload)

    job_id = _upload(client, token, "讲义.pdf", b"%PDF").json()["job"]["id"]
    job = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert job["status"] == "done"
    assert job["parsed_chunks"] == 3
    assert job["indexed_chunks"] == 2
    assert job["duplicate_chunks"] == 1


def test_new_content_is_retrievable_right_after_ingest(admin_client, monkeypatch):
    """入库完要能立刻检索到 —— 引擎缓存不重置的话会一直指着旧连接。"""
    client, token, _ = admin_client
    from backend.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "parse_one",
                        lambda item, raw_dir: [_chunk("拥塞控制的慢启动阶段窗口指数增长")])
    assert retriever.get_engine().has_corpus() is False

    _upload(client, token, "拥塞控制.pdf", b"%PDF")
    engine = retriever.get_engine()
    assert engine.has_corpus() is True
    hits = engine.retrieve("慢启动", course="net")
    assert hits and "慢启动" in hits[0].text


# --------------------------------------------------------------------------
# 4. 新开的课也能传资料
# --------------------------------------------------------------------------
def test_a_newly_opened_course_accepts_uploads(admin_client):
    """课程搬进数据库之后，新课的目录名就是 course_id，扫描要认得。"""
    client, token, tmp_path = admin_client
    client.post("/admin/courses", json={
        "id": "discrete", "display": "《离散数学》", "short": "离散", "score": 40,
        "verification_types": ["rule"],
        "knowledge_taxonomy": [{"chapter": "图论", "topics": ["欧拉图"]}],
    }, headers=_auth(token))

    r = _upload(client, token, "图论.pdf", b"%PDF", course="discrete")
    assert r.status_code == 200, r.text
    assert (tmp_path / "raw" / "discrete" / "courseware" / "图论.pdf").is_file()

    from backend.rag.ingest import _course_dirs
    assert "discrete" in _course_dirs(), "全量重建时要扫得到新课的目录"


# --------------------------------------------------------------------------
# 5. 撤下一份资料：断开检索 + 列表隐去，文件留在服务器上
# --------------------------------------------------------------------------
def test_withdraw_keeps_the_file_but_moves_it_out_of_the_corpus(admin_client, monkeypatch):
    """撤下之后：索引里没了、默认列表里没了，**文件还在**，只是不在 data/raw 下。"""
    client, token, tmp_path = admin_client
    from backend.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "parse_one",
                        lambda item, raw_dir: [_chunk("滑动窗口协议的发送方缓冲区",
                                                      source=item["name"])])
    job_id = _upload(client, token, "讲义.pdf", b"%PDF").json()["job"]["id"]
    origin = tmp_path / "raw" / "net" / "courseware" / "讲义.pdf"
    assert origin.is_file()

    r = client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_chunks"] == 1 and body["file_kept"] is True

    assert not origin.exists(), "必须移出 data/raw，否则整轮重建会把它索引回去"
    parked = tmp_path / "withdrawn" / "net" / "courseware" / "讲义.pdf"
    assert parked.is_file(), "文件本身要留着"
    assert parked.read_bytes() == b"%PDF", "留下的必须是原件，不是空壳"


def test_withdrawn_job_leaves_the_default_list_but_stays_findable(admin_client):
    """默认列表里不显示；单独查 withdrawn=true 还能看到，按 id 也仍取得到。"""
    client, token, _ = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"junk").json()["job"]["id"]
    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))

    listed = client.get("/admin/materials", headers=_auth(token)).json()["items"]
    assert [j["id"] for j in listed] == [], "撤下的不该出现在默认列表里"

    hidden = client.get("/admin/materials?withdrawn=true", headers=_auth(token)).json()["items"]
    assert [j["id"] for j in hidden] == [job_id]
    assert hidden[0]["status"] == "withdrawn" and hidden[0]["withdrawn_at"]

    got = client.get(f"/admin/materials/{job_id}", headers=_auth(token))
    assert got.status_code == 200, "行不能删 —— 删了就无从知道盘上那份文件是什么"


def test_withdrawn_content_is_no_longer_retrievable(admin_client, monkeypatch):
    """这条是撤下的全部意义 —— 学生立刻检索不到它。"""
    client, token, _ = admin_client
    from backend.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "parse_one",
                        lambda item, raw_dir: [_chunk("拥塞避免阶段窗口线性增长",
                                                      source=item["name"])])
    job_id = _upload(client, token, "旧版.pdf", b"%PDF").json()["job"]["id"]
    assert retriever.get_engine().retrieve("拥塞避免", course="net")

    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    assert not retriever.get_engine().retrieve("拥塞避免", course="net")


def test_a_full_rebuild_does_not_resurrect_withdrawn_material(admin_client):
    """整轮重建扫的是 data/raw —— 撤下的文件必须扫不到。

    这是「移出去」而不是「留在原地」的唯一理由：不然下一次 build_fts_from_raw
    会把撤下的资料原样索引回去，中间不会有任何提示，撤下就成了假动作。
    """
    from pathlib import Path

    from backend.rag.ingest import discover_corpus

    client, token, tmp_path = admin_client
    job_id = _upload(client, token, "旧版.pdf", b"%PDF").json()["job"]["id"]
    assert any(i["name"] == "旧版" for i in discover_corpus(Path(tmp_path / "raw")))

    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    assert not any(i["name"] == "旧版" for i in discover_corpus(Path(tmp_path / "raw")))


def test_withdraw_then_reupload_the_same_name_works(admin_client, monkeypatch):
    """「同名不覆盖」的 409 得有出路：撤下旧版就能传新版，且内容真的换了。"""
    client, token, _ = admin_client
    from backend.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "parse_one",
                        lambda item, raw_dir: [_chunk("第一版的内容", source=item["name"])])
    job_id = _upload(client, token, "讲义.pdf", b"%PDF").json()["job"]["id"]
    assert _upload(client, token, "讲义.pdf", b"%PDF").status_code == 409

    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    monkeypatch.setattr(ingest_mod, "parse_one",
                        lambda item, raw_dir: [_chunk("第二版的内容", source=item["name"])])
    assert _upload(client, token, "讲义.pdf", b"%PDF").status_code == 200

    engine = retriever.get_engine()
    assert engine.retrieve("第二版", course="net")
    assert not engine.retrieve("第一版", course="net")


def test_withdrawing_the_same_name_twice_keeps_both_files(admin_client):
    """同一个名字撤下两次，先撤的那份不能被后撤的覆盖掉。"""
    client, token, tmp_path = admin_client
    first = _upload(client, token, "讲义.pdf", b"first").json()["job"]["id"]
    client.delete(f"/admin/materials/{first}", headers=_auth(token))
    second = _upload(client, token, "讲义.pdf", b"second").json()["job"]["id"]
    client.delete(f"/admin/materials/{second}", headers=_auth(token))

    parked = sorted((tmp_path / "withdrawn" / "net" / "courseware").iterdir())
    assert len(parked) == 2, f"两份都要在，实际 {[p.name for p in parked]}"
    assert {p.read_bytes() for p in parked} == {b"first", b"second"}


def test_withdraw_only_touches_the_named_doc_type(admin_client, monkeypatch):
    """source 是不带后缀的文件名，同课程不同类别下会撞名 —— 别把邻居撤了。"""
    client, token, _ = admin_client
    from backend.rag import ingest as ingest_mod

    # 真的 parse_one 会把 item 的 name 与 doc_type 都带进分块 —— 假的也必须带，
    # 否则两份文件的分块看起来是同一个 (source, doc_type)，测不出这条用例要测的东西
    monkeypatch.setattr(ingest_mod, "parse_one", lambda item, raw_dir: [
        _chunk(f"{item['doc_type']} 里的那一段", source=item["name"],
               doc_type=item["doc_type"])])
    _upload(client, token, "第3章.pdf", b"%PDF", doc_type="courseware")
    keep = _upload(client, token, "第3章.pdf", b"%PDF", doc_type="textbook").json()["job"]["id"]

    first = client.get("/admin/materials", headers=_auth(token)).json()["items"]
    victim = next(j["id"] for j in first if j["doc_type"] == "courseware")
    client.delete(f"/admin/materials/{victim}", headers=_auth(token))

    hits = retriever.get_engine().retrieve("那一段", course="net")
    assert [h.doc_type for h in hits] == ["textbook"]
    assert client.get(f"/admin/materials/{keep}", headers=_auth(token)).json()["status"] == "done"


def test_withdraw_of_a_failed_job_still_frees_the_name(admin_client):
    """解析失败的那份也要能撤下 —— 文件在盘上占着名字，不撤就再也传不进去。"""
    client, token, tmp_path = admin_client
    job_id = _upload(client, token, "坏文件.pdf", b"junk").json()["job"]["id"]
    assert client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()["status"] == "failed"

    body = client.delete(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert body["deleted_chunks"] == 0 and body["file_kept"] is True
    assert body["note"], "索引里本就没有分块这件事要说出来，不能静默成功"
    assert not (tmp_path / "raw" / "net" / "courseware" / "坏文件.pdf").exists()
    assert (tmp_path / "withdrawn" / "net" / "courseware" / "坏文件.pdf").is_file()


def test_withdrawing_twice_is_refused(admin_client):
    client, token, _ = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"junk").json()["job"]["id"]
    assert client.delete(f"/admin/materials/{job_id}", headers=_auth(token)).status_code == 200
    assert client.delete(f"/admin/materials/{job_id}", headers=_auth(token)).status_code == 409


def test_withdraw_is_admin_only(admin_client):
    client, token, _ = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"junk").json()["job"]["id"]
    student = issue_token("stu", "student")
    assert client.delete(f"/admin/materials/{job_id}", headers=_auth(student)).status_code == 403
    assert client.get(f"/admin/materials/{job_id}", headers=_auth(token)).status_code == 200


def test_withdraw_of_unknown_job_is_404(admin_client):
    client, token, _ = admin_client
    assert client.delete("/admin/materials/99999", headers=_auth(token)).status_code == 404


def test_restore_puts_the_file_back_and_reindexes(admin_client, monkeypatch):
    """放回去：文件搬回 data/raw，重新解析入库，学生又能检索到。"""
    client, token, tmp_path = admin_client
    from backend.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "parse_one",
                        lambda item, raw_dir: [_chunk("三次握手建立连接", source=item["name"])])
    job_id = _upload(client, token, "传输层.pdf", b"%PDF").json()["job"]["id"]
    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    assert not retriever.get_engine().retrieve("三次握手", course="net")

    r = client.post(f"/admin/materials/{job_id}/restore", headers=_auth(token))
    assert r.status_code == 200, r.text
    job = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert job["status"] == "done" and job["indexed_chunks"] == 1
    assert not job["withdrawn_at"]
    assert (tmp_path / "raw" / "net" / "courseware" / "传输层.pdf").is_file()
    assert retriever.get_engine().retrieve("三次握手", course="net")

    listed = client.get("/admin/materials", headers=_auth(token)).json()["items"]
    assert [j["id"] for j in listed] == [job_id], "放回后要回到默认列表"


def test_restore_refuses_to_clobber_a_newer_file_of_the_same_name(admin_client):
    """撤下腾出了文件名，这期间传了新版 —— 放回旧版不能把新版覆盖掉。"""
    client, token, _ = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"old").json()["job"]["id"]
    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    _upload(client, token, "讲义.pdf", b"new")

    r = client.post(f"/admin/materials/{job_id}/restore", headers=_auth(token))
    assert r.status_code == 409 and "同名" in r.json()["detail"]


def test_restore_of_a_job_that_was_not_withdrawn_is_refused(admin_client):
    client, token, _ = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"junk").json()["job"]["id"]
    assert client.post(f"/admin/materials/{job_id}/restore",
                       headers=_auth(token)).status_code == 409


def test_restore_says_so_when_the_file_was_cleaned_up_by_hand(admin_client):
    """有人上服务器把停放区清了 —— 要说清楚是文件没了，不是接口坏了。"""
    client, token, tmp_path = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"junk").json()["job"]["id"]
    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    (tmp_path / "withdrawn" / "net" / "courseware" / "讲义.pdf").unlink()

    r = client.post(f"/admin/materials/{job_id}/restore", headers=_auth(token))
    assert r.status_code == 410 and "重新上传" in r.json()["detail"]


def test_restore_is_admin_only(admin_client):
    client, token, _ = admin_client
    job_id = _upload(client, token, "讲义.pdf", b"junk").json()["job"]["id"]
    client.delete(f"/admin/materials/{job_id}", headers=_auth(token))
    student = issue_token("stu", "student")
    assert client.post(f"/admin/materials/{job_id}/restore",
                       headers=_auth(student)).status_code == 403


# --------------------------------------------------------------------------
# 6. Word 与纯文本
# --------------------------------------------------------------------------
def _docx_bytes(paragraphs: list[tuple[str, str]]) -> bytes:
    """造一份真的 .docx。paragraphs 是 (样式名, 文本)，样式名空串表示正文。"""
    import io

    from docx import Document

    doc = Document()
    for style, text in paragraphs:
        doc.add_paragraph(text, style=style or None)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_docx_is_chunked_by_heading_and_retrievable(admin_client):
    """Word 走完整条链路：解析 → 按标题切 → 入库 → 立刻检索得到。"""
    client, token, _ = admin_client
    payload = _docx_bytes([
        ("Heading 1", "第3章 数据链路层"),
        ("", "本章讨论点对点信道与广播信道。" * 12),
        ("Heading 2", "3.2 CSMA/CD 协议"),
        ("", "适配器边发送边检测碰撞，一旦检测到碰撞就立即停止发送。" * 12),
    ])
    job_id = _upload(client, token, "网络讲义.docx", payload).json()["job"]["id"]
    job = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert job["status"] == "done", job["error"]
    assert job["indexed_chunks"] >= 2

    hits = retriever.get_engine().retrieve("检测到碰撞", course="net")
    assert hits, "Word 里的内容必须能检索到"
    top = hits[0]
    assert "第3章 数据链路层" in top.title_path and "CSMA/CD" in top.title_path
    assert top.page_start == 0, "Word 没有页码，不能编一个出来"


def test_docx_citation_says_nothing_about_pages(admin_client):
    """没有页码时引用只给标题路径 —— 不能出现「第 0 页」。"""
    client, token, _ = admin_client
    payload = _docx_bytes([("Heading 1", "第1章 概述"), ("", "计算机网络的性能指标。" * 20)])
    _upload(client, token, "概述.docx", payload)

    hit = retriever.get_engine().retrieve("性能指标", course="net")[0]
    label = hit.citation() if hasattr(hit, "citation") else str(hit.to_dict())
    assert "第 0 页" not in str(label)


def test_docx_tables_keep_their_place_in_the_text(admin_client):
    """表格要按它在文档里的位置进正文，不能被堆到末尾 —— 习题类文档全靠这个。"""
    import io

    from docx import Document

    doc = Document()
    doc.add_paragraph("第2章 传输层", style="Heading 1")
    doc.add_paragraph("下表比较两种协议：" * 8)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "TCP"
    table.cell(0, 1).text = "面向连接可靠传输"
    table.cell(1, 0).text = "UDP"
    table.cell(1, 1).text = "无连接开销小"
    buf = io.BytesIO()
    doc.save(buf)

    client, token, _ = admin_client
    _upload(client, token, "传输层.docx", buf.getvalue())

    hits = retriever.get_engine().retrieve("无连接开销小", course="net")
    assert hits, "表格里的文字也要进索引"
    assert "第2章 传输层" in hits[0].title_path, "表格要归到它上面那个标题下"


def test_gbk_encoded_txt_is_not_mojibake(admin_client):
    """GBK 的 txt 必须解对。解错不会报错，只会安静地进一堆乱码然后永远检不到。"""
    client, token, _ = admin_client
    body = "第4章 网络层\n" + "路由选择协议分为内部网关协议和外部网关协议。" * 10
    job_id = _upload(client, token, "网络层.txt", body.encode("gbk")).json()["job"]["id"]
    assert client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()["status"] == "done"

    hits = retriever.get_engine().retrieve("内部网关协议", course="net")
    assert hits, "GBK 文本被当成 utf-8 解的话，这里会是空的"
    assert "第4章 网络层" in hits[0].title_path


def test_txt_headings_are_guessed_from_line_shape(admin_client):
    """纯文本没有样式，「第X章」「3.2 xxx」这两个形状要认出来当标题。"""
    client, token, _ = admin_client
    body = ("第5章 应用层\n" + "应用层协议定义了进程间通信的规则。" * 8 + "\n"
            + "5.2 域名系统 DNS\n" + "DNS 使用分布式的层次数据库。" * 8)
    _upload(client, token, "应用层.txt", body.encode("utf-8"))

    hits = retriever.get_engine().retrieve("分布式的层次数据库", course="net")
    assert hits and hits[0].title_path == "第5章 应用层 / 5.2 域名系统 DNS"


def test_upload_and_rebuild_accept_the_same_extensions():
    """上传接口与整轮重建认的后缀必须一致。

    这两份名单在不同文件里（admin_routes._EXT_PARSER 按后缀选 parser，
    ingest.EXT_PARSER 供 discover_corpus 扫目录）。只加一边的后果很隐蔽：
    教务传上来的 Word 进得了索引，但下一次 build_fts_from_raw 整轮重建会扫不到
    它 —— 索引里那些块就此消失，而且没有任何报错，列表里也看不出少了什么。
    """
    from backend.api.admin_routes import _EXT_PARSER
    from backend.rag.ingest import EXT_PARSER

    assert set(_EXT_PARSER) == set(EXT_PARSER), (
        f"上传认 {sorted(_EXT_PARSER)}，重建认 {sorted(EXT_PARSER)}")


def test_old_doc_format_is_rejected_with_a_useful_hint(admin_client):
    """.doc 看着就是 Word，拒绝时必须说清楚是格式老了，不是"不支持 Word"。"""
    client, token, _ = admin_client
    r = _upload(client, token, "讲义.doc", b"\xd0\xcf\x11\xe0old-binary-doc")
    assert r.status_code == 400
    assert ".docx" in r.json()["detail"], r.text


def test_empty_reason_is_specific_to_the_format(admin_client):
    """解析出 0 块的原因按格式分开说 —— 对 Word 讲"请先 OCR"会把人引到错路上。"""
    client, token, _ = admin_client
    # 一份合法但整页没有文字的 PDF —— 这正是扫描件在解析器眼里的样子。
    # 用 b"junk" 不行：那是"文件损坏"，走的是抛异常那条路，不是解析出 0 块。
    import fitz

    doc = fitz.open()
    doc.new_page()
    pdf = _upload(client, token, "扫描件.pdf", doc.tobytes()).json()["job"]["id"]
    docx = _upload(client, token, "空的.docx", _docx_bytes([])).json()["job"]["id"]

    pdf_err = client.get(f"/admin/materials/{pdf}", headers=_auth(token)).json()["error"]
    docx_err = client.get(f"/admin/materials/{docx}", headers=_auth(token)).json()["error"]
    assert "OCR" in pdf_err
    assert "OCR" not in docx_err and docx_err, docx_err


# --------------------------------------------------------------------------
# 3. 扫描件 OCR 兜底
# --------------------------------------------------------------------------
def _textless_pdf() -> bytes:
    """一份真 PDF，但没有文本层 —— 扫描件在解析器眼里就长这样。

    不能用 b"junk" 冒充：那是**打不开**的文件，走的是异常分支，
    与"打得开、但一个字都提不出来"是两种不同的处置。
    """
    import fitz

    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_scanned_pdf_falls_back_to_ocr(admin_client, monkeypatch):
    """数字版解析出 0 块 → 当扫描件走 OCR，转写结果照常进索引。

    接这条路之前，扫描件传上来一律落 failed 并提示"需先 OCR"——教务得自己找一个
    OCR 工具、转成数字版 PDF 再传一次。而 `scripts/vlm_ocr_test.py` 早就把这条路
    走通了（`data/ocr` 那 53 页就是它转的），只是没接进来。
    """
    from backend.rag import ocr as ocr_mod
    from backend.rag.chunking import Chunk

    client, token, _ = admin_client
    monkeypatch.setattr(settings, "llm_vision_model", "stub-vision")
    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")
    monkeypatch.setattr(ocr_mod, "ocr_chunks", lambda *a, **k: (
        [Chunk(text="OCR 转写出来的内容", source="扫描讲义", page_start=1, page_end=1,
               doc_type="courseware", course="net")],
        {"total_pages": 3, "ocr_pages": 3, "failed_pages": 0, "truncated": False}))

    job_id = _upload(client, token, "扫描讲义.pdf", _textless_pdf()).json()["job"]["id"]
    job = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert job["status"] == "done"
    assert job["indexed_chunks"] == 1
    # 必须说出"这是 OCR 转写的"：检索引到它时，人得有机会怀疑一下转写误差
    assert "OCR 转写入库" in job["error"]


def test_ocr_note_says_when_only_part_of_the_file_was_transcribed(admin_client, monkeypatch):
    """只转写了前 N 页却在列表上显示"已入库"，是最难查的一类问题。"""
    from backend.rag import ocr as ocr_mod
    from backend.rag.chunking import Chunk

    client, token, _ = admin_client
    monkeypatch.setattr(settings, "llm_vision_model", "stub-vision")
    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")
    monkeypatch.setattr(ocr_mod, "ocr_chunks", lambda *a, **k: (
        [Chunk(text="前五十页", source="厚教材", page_start=1, page_end=50,
               doc_type="courseware", course="net")],
        {"total_pages": 200, "ocr_pages": 50, "failed_pages": 2, "truncated": True}))

    job_id = _upload(client, token, "厚教材.pdf", _textless_pdf()).json()["job"]["id"]
    note = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()["error"]
    assert "只转写了前 50 页" in note and "200 页" in note
    assert "2 页转写失败" in note


def test_without_a_vision_endpoint_the_old_advice_still_stands(admin_client, monkeypatch):
    """没配视觉端点时，"请先 OCR 成数字版 PDF"仍是对的处置，不能被这条新路盖掉。"""
    client, token, _ = admin_client
    monkeypatch.setattr(settings, "llm_vision_model", "")
    job_id = _upload(client, token, "扫描件.pdf", _textless_pdf()).json()["job"]["id"]
    job = client.get(f"/admin/materials/{job_id}", headers=_auth(token)).json()
    assert job["status"] == "failed"
    assert "需要先 OCR" in job["error"]


def test_one_bad_page_does_not_lose_the_whole_document(monkeypatch, tmp_path):
    """逐页失败只丢那一页。整份重来的代价是把已经转写好的页数再买一遍。"""
    from backend.rag import ocr as ocr_mod

    calls = {"n": 0}

    def _flaky(prompt, url):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("网关超时")
        return f"第 {calls['n']} 页的文字"

    monkeypatch.setattr(settings, "logs_dir", str(tmp_path))
    monkeypatch.setattr(ocr_mod, "llm_ask_vision", _flaky)
    monkeypatch.setattr(ocr_mod, "render_page", lambda path, no, dpi=150: b"png")
    monkeypatch.setattr("fitz.open", lambda path: type(
        "D", (), {"page_count": 3, "close": lambda self: None})())

    pages, stat = ocr_mod.transcribe_pdf("whatever.pdf")
    assert stat == {"total_pages": 3, "ocr_pages": 3, "failed_pages": 1, "truncated": False}
    assert pages[0] and pages[2] and pages[1] == ""
