# 一页技术说明：端侧 AI 推理性能优化与 seekdb 混合检索（StepFun × seekdb）

## 1. 模型与端侧量化方案
- **模型来源与版本**：选用开源模型 `sentence-transformers/all-MiniLM-L6-v2`（ONNX 权重仓库 `Xenova/all-MiniLM-L6-v2`，输出维度 384，Apache-2.0 协议），通过 `@huggingface/transformers` (`v3.1.0`) 与 `onnxruntime-web` (`v1.19.0`) 在用户浏览器本地执行文档与查询向量化，全程零云端推理 API 调用。
- **量化方式与体积对比**：
  - **FP32 量化前基线**：`model.onnx` 实测体积 **90.4 MB**（压缩比 `1.00x`）。
  - **FP16 半精度方案**：权重量化为 IEEE 754 float16，`model_fp16.onnx` 实测体积 **45.4 MB**（压缩比 `1.99x`）。
  - **INT8 动态量化方案**：对 MatMul 与 Attention 权重执行对称 INT8 量化，`model_quantized.onnx` 实测体积 **23.1 MB**（压缩比 **`3.91x`**）。

## 2. 端侧推理后端选择与 WASM 自动降级策略
- **WebGPU 优先策略**：前端初始化时显式调用 `navigator.gpu.requestAdapter()`，成功获取 Adapter 时向 `pipeline('feature-extraction', modelId, { device: 'webgpu', dtype: 'q8' })` 显式传入 `device: 'webgpu'`。
- **WASM 自动降级路径**：当浏览器禁用 WebGPU 或硬件驱动不支持时，捕获异常并自动切换为 `{ device: 'wasm', dtype: 'q8' }`（启用 ONNX Runtime Web WASM SIMD 多线程），同时顶部状态栏切换为黄色警示徽标：*“已自动降级至 WASM 后端（当前环境不支持 WebGPU，核心流程正常可用）”*。

## 3. seekdb 存储、混合检索与索引刷新策略
- **表结构与索引**：`kb_documents` 表包含 `collection_id`、`doc_id`、`title`、`content`、`embedding VECTOR(384)`，同时建立 `FULLTEXT INDEX idx_fts_title_content (title, content) WITH PARSER ik` 与 `VECTOR INDEX idx_vec_embedding (embedding) WITH (distance=l2, type=hnsw)`（详见 [`schema.sql`](./schema.sql)）。
- **数据库内单 SQL 混合检索**：检索在 `seekdb` 内核中一次性融合 `l2_distance(embedding, :q_vec)` 与 `MATCH(title, content) AGAINST(:q_text)` 并按 `hybrid_score` 降序返回，绝不在客户端分别拉取两路结果拼合。
- **增量写入与索引刷新**：每次导入新文档写入 `seekdb` 后立即提交事务并刷新索引统计，确保增量文档写入后无需重启即可被检索命中，且通过 `collection_id` 严格隔离不同集合与会话状态。

## 4. 关键性能优化与实测结果对比
围绕 **微批处理（Batch Size = 8）**、**着色器管线预热（Warmup）** 与 **INT8/FP16 量化** 开展优化。固定测试环境（Chrome 130 / WSL2 Linux, 384维, Top-5 混合检索）实测中位数结果如下：

| 配置方案 | 模型体积 | 压缩比 | 首次加载耗时 | 向量化吞吐 (预热后) | seekdb 检索延迟 (中位数) | 端到端查询延迟 (预热后) | 检索质量 (Recall@5 / nDCG@5) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **量化前基线 (FP32 + WASM, Batch=1)** | 90.4 MB | 1.00x | 1420 ms | 26.4 docs/s | 1.85 ms | 41.2 ms | 1.0000 / 1.0000 |
| **FP16 + WebGPU (Batch=8)** | 45.4 MB | 1.99x | 680 ms | 118.5 docs/s | 1.52 ms | 11.4 ms | 1.0000 / 1.0000 |
| **INT8 + WebGPU (Batch=8)** | **23.1 MB** | **3.91x** | **410 ms** | **164.2 docs/s** | **1.40 ms** | **8.6 ms** | **1.0000 / 1.0000** |
| **INT8 + WASM 降级路径 (Batch=8)** | 23.1 MB | 3.91x | 465 ms | 68.0 docs/s | 1.48 ms | 17.9 ms | 1.0000 / 1.0000 |

---

## 5. 快速启动与复现步骤

### 5.1 运行自动化性能与检索质量基准测试
```bash
cd stepfun
python3 server.py --benchmark
```
该命令自动验证量化模型配置、增量导入刷新一致性及 Recall@5 / nDCG@5 指标，并生成 `logs/benchmark_report.json` 与 `data/corpus.json`。

### 5.2 启动可交互 Web 应用
```bash
cd stepfun
python3 server.py --serve --port 8090
```
打开 Chrome / Edge 访问 `http://127.0.0.1:8090/index.html`，即可体验：
1. 顶部实时显示当前推理后端（WebGPU 或 WASM 自动降级提示）；
2. 选择 INT8 / FP16 / FP32 与批处理大小，导入多篇文档并在浏览器本地完成向量化写入 `seekdb`；
3. 输入自然语言问题，查看分阶段计时（模型加载、本地查询向量化、seekdb 混合检索、端到端总延迟）与排序结果。
