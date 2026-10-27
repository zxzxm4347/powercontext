-- seekdb 向量与全文混合检索集合表定义 (兼容 seekdb embedded / server 模式)
CREATE DATABASE IF NOT EXISTS stepfun_edge_kb;
USE stepfun_edge_kb;

CREATE TABLE IF NOT EXISTS kb_documents (
    doc_id VARCHAR(128) NOT NULL,
    collection_id VARCHAR(64) NOT NULL DEFAULT 'default',
    session_id VARCHAR(64) NOT NULL DEFAULT 'session_01',
    title VARCHAR(512) NOT NULL,
    content TEXT NOT NULL,
    quant_mode VARCHAR(16) NOT NULL DEFAULT 'INT8',
    backend VARCHAR(16) NOT NULL DEFAULT 'WebGPU',
    embedding VECTOR(384) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collection_id, doc_id),
    FULLTEXT INDEX idx_fts_title_content (title, content) WITH PARSER ik,
    VECTOR INDEX idx_vec_embedding (embedding) WITH (distance=l2, type=hnsw, m=16, ef_construction=200)
);

-- 数据库内单 SQL 向量 + 全文混合检索查询模板
-- SELECT doc_id, title, content,
--        MATCH(title, content) AGAINST(? IN NATURAL LANGUAGE MODE) AS fts_score,
--        l2_distance(embedding, ?) AS vec_distance,
--        (0.65 / (1.0 + l2_distance(embedding, ?)) + 0.35 * LEAST(MATCH(title, content) AGAINST(? IN NATURAL LANGUAGE MODE), 5.0) / 5.0) AS hybrid_score
-- FROM kb_documents
-- WHERE collection_id = ?
-- ORDER BY hybrid_score DESC
-- LIMIT ?;
