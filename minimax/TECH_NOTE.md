# 一页技术说明：面向软件持续演进的 SDD 上下文管理系统

## 1. 模块架构
系统由三层组成：
1. **工程资料存储与检索层（`SeekDBEngineeringIndex`）**：将 Markdown 规格文档按条款（`[SPEC-ORD-01]`、`[SPEC-ORD-02]`）切分，连同代码函数签名（`workspace/order_service.py::calculate_order_quote`）与单元测试类（`TestGreenSubsidyFeature`、`TestOrderRegression`）一同编码为向量并存入 `seekdb`（表 `sdd_engineering_assets`）。
2. **跨运行期状态管理层（`PowerContextTaskStateStore`）**：以项目 `scope_sdd_order_project` 为隔离单元，在 `PowerContext` 中持久化记录任务的 `completed_items`、`todo_items` 与 `verification_result`。模块重启后优先加载已有状态并合并到开发上下文中。
3. **MiniMax 协同修改与回归验证层**：将组装好的结构化上下文提供给 `MiniMax-Text-01`，生成针对 `calculate_order_quote` 的最小侵入式补丁，并通过 `unittest` 执行修改前后的对比验证。

## 2. 上下文选择与规格动态演进机制
- **按任务语义检索**：输入任务描述后，`SeekDBEngineeringIndex.query_context_for_task` 计算查询向量与工程资产的距离，返回关联度最高的主体规格条款、待改代码文件及两类测试（功能测试 + 回归测试）。
- **规格热更新感知**：当规格文档演进为 `specs/order_discount_spec_v2.md` 并重新执行 `ingest_spec` 后，原条款记录的版本号更新为 `v2.0`，后续上下文查询自动输出更新后的 `0.20` 折扣封顶规则，杜绝静态缓存导致的过时约束。

## 3. 各技术组件分工总结
- **seekdb / OceanBase**：工程规格、代码符号、测试套件的结构化与向量化持久化检索。
- **PowerContext**：任务执行生命周期的状态保存、断点恢复与跨会话交接。
- **MiniMax**：严格遵循 SDD 上下文中的新规格条款与回归不变量完成代码生成与修改。
