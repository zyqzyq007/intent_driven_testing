# Intent-Driven Testing Pipeline (Step 1 to 5)

本文档详细说明当前 `intent_driven_testing` 项目中，前 5 个步骤（输入转换、ESG 构建、意图生成、测试用例生成、测试执行与修复）的**实现方式**、**数据契约**、**关键算法**与**维护建议**。

---

## 1. 总览

当前流水线入口为 `run_pipeline.py`，支持按步骤执行：

- Step 1：`pipeline/step1_input_transform/extractor.py`
- Step 2：`pipeline/step2_esg_construction/esg_runner.py`
- Step 3：`pipeline/step3_intent_generation/*`
- Step 4：`pipeline/step4_test_generation/*`
- Step 5：`pipeline/step5_test_execution/*`

核心数据流：

1. Step 1 读取 Java 项目源码，输出 `pairs.json`
2. Step 2 调用 Java ESG 分析器，输出 `esg_graph.json`（及 `spark_esg.dot`）
3. Step 3 融合 `pairs.json + esg_graph.json`，输出 `intents.json`
4. Step 4 融合 `intents.json + pairs.json`，构建完整上下文 Prompt 并调用 LLM 生成测试代码，输出 `generated_tests.json`
5. Step 5 读取 `generated_tests.json`，隔离运行 Maven 测试，结合编译/运行错误利用 LLM 进行最多 3 次自我修复，输出 `execution_results.json`

---

## 2. 目录与模块职责

### 2.1 Python Pipeline

- `run_pipeline.py`  
  顶层编排：参数解析、步骤选择、结果摘要打印。

- `pipeline/step1_input_transform/extractor.py`  
  扫描 `src/main/java` 与 `src/test/java`，提取“测试方法 ↔ 被测方法”配对。

- `pipeline/step2_esg_construction/esg_runner.py`  
  负责 Maven 编译/执行 Java ESG 模块，并加载 JSON 图。

- `pipeline/step3_intent_generation/models.py`  
  Step 3 全部数据模型（slice、intent、record、context_code）。

- `pipeline/step3_intent_generation/esg_extractor.py`  
  从 ESG 图中提取 focal method 的行为语义切片（BehavioralSemanticSlice）。

- `pipeline/step3_intent_generation/intent_builder.py`  
  规则驱动生成三类意图骨架（GWT）。

- `pipeline/step3_intent_generation/code_resolver.py`  
  解析并补全上下文代码（相关方法源码、字段定义、imports）。

- `pipeline/step3_intent_generation/generator.py`  
  Step 3 主流程：extract slice → resolve context → build intents → save。

- `pipeline/step4_test_generation/prompt_builder.py`  
  基于提取的意图和上下文代码，构造符合大模型（LLM）输入的结构化 GWT 提示词。

- `pipeline/step4_test_generation/generator.py`  
  Step 4 主流程：读取意图 → 检索相似用例 (Few-Shot) → 构建 Prompt → 调用 DeepSeek API 生成测试代码。

### 2.2 Java ESG 模块

- `esg_construction/src/main/java/com/esg/Main.java`  
  ESG 分析入口。接收 `[targetClassesDir] [outputDir]`，输出：
  - `spark_esg.dot`
  - `esg_graph.json`

---

## 3. Step 1：Input Transformation（实现细节）

文件：`pipeline/step1_input_transform/extractor.py`

### 3.1 目标

从 Java 项目中提取结构化对：

```json
{
  "test_class": "...",
  "test_method": "...",
  "test_file_path": "...",
  "test_code": "...",
  "focal_class": "...",
  "focal_method": "...",
  "focal_file_path": "...",
  "focal_code": "..."
}
```

### 3.2 扫描策略

1. 优先 Maven 标准目录：
   - main: `src/main/java`
   - test: `src/test/java`
2. 若不存在，回退到项目根目录递归扫描 `*.java`

### 3.3 AST 解析

- 使用 `javalang.parse.parse(content)` 解析文件
- 仅索引：
  - main 中非 test class
  - test 中 test class

### 3.4 Test/Focal 判定逻辑

- Test class 判定：
  - 类名后缀 `Test` / `Tests`
  - 或类内方法含 `@Test` / `@ParameterizedTest`

- Test method 判定：
  - 注解 `@Test` / `@ParameterizedTest`
  - 或方法名 `test*`

### 3.5 Test 方法到 focal 方法映射

`_guess_focal_method(test_method_name, focal_methods)`：

1. 去掉 `test` / `test_` 前缀后精确匹配（忽略大小写）
2. 子串匹配（例如 `testEncodeBase64` → `encode`）
3. 当前版本无 LCS fallback（注释有提及，实际实现到子串）

### 3.6 源码片段抽取

- `_extract_method_source` 使用**大括号计数**，从方法起始行向下截取完整方法体  
- 这是 Step 1/3 都复用的稳定抽取策略

---

## 4. Step 2：ESG Construction（实现细节）

文件：`pipeline/step2_esg_construction/esg_runner.py`  
Java入口：`src/ESG_construction/.../Main.java`

### 4.1 目标

构建 Execution Semantic Graph（ESG）并落盘为 JSON。

### 4.2 执行流程

1. 检查被测项目 `target/classes` 是否存在（必须先编译被测项目）
2. 可选编译 ESG 模块：`mvn compile`
3. 运行 ESG 分析器：`mvn exec:java -Dexec.mainClass=com.esg.Main`
4. 加载 `esg_graph.json` 并做基础统计校验

### 4.3 Java Main 输出契约

`Main.java` 输出 schema：

```json
{
  "nodes": [
    {"id": "...", "label": "...", "type": "METHOD|STATE|DATA", "allocation_site": "...?"}
  ],
  "edges": [
    {"source": "...", "target": "...", "edge_type": "TEMPORAL|STATE_TRANSITION|CAUSAL", "label": "..."}
  ]
}
```

并同时输出 `spark_esg.dot` 供可视化检查。

### 4.4 当前数据规模（spark-master）

- 节点：1615
  - METHOD: 1039
  - STATE: 325
  - DATA: 251
- 边：3611
  - CAUSAL: 2307
  - TEMPORAL: 1278
  - STATE_TRANSITION: 26

---

## 5. Step 3：Intent Generation（实现细节）

### 5.1 总体目标

输入：

- `pairs.json`（Step 1）
- `esg_graph.json`（Step 2）

输出：

- `intents.json`（每条 pair 对应一条 IntentRecord，含语义切片 + GWT intents + 代码上下文）

---

### 5.2 数据模型（`models.py`）

关键模型：

- `BehavioralSemanticSlice`
  - `prerequisite_states`
  - `preceding_calls`
  - `data_reads`
  - `data_writes`
  - `post_state_effects`
  - `downstream_calls`

- `IntentSkeleton`
  - `intent_type`（Functional / Boundary/Exception / Interaction/Dependency）
  - `given / when / then`
  - `slice_summary`

- `ContextCode`（用于 Step 4 生成测试）
  - `focal_code`
  - `related_method_codes`
  - `field_definitions`
  - `focal_class_imports`

- `IntentRecord`
  - 元信息（pair_id/test_class/test_method/focal_class/focal_method）
  - `context_code`
  - `semantic_slice`
  - `intents`

> 当前已去除 `test_code`，避免旧测试对 Step 4 造成干扰。

---

### 5.3 ESG 切片提取（`esg_extractor.py`）

#### 5.3.1 focal method 定位

`ESGGraph.find_method_node(class_name, method_name)`：

- 首先匹配：
  - `node.type == METHOD`
  - `node.label == method_name`
  - `node.id` 包含 `.ClassName:`
- 多候选时用度数（入+出边数）择优

#### 5.3.2 入边提取（Given）

- `STATE -> METHOD` 且 label 包含 `guarded_by*`  
  → `prerequisite_states`
- `METHOD -> METHOD` 且 label 包含 `follows_in_*`（作为当前方法的入边）  
  → `preceding_calls`
- `DATA -> METHOD` 且 label in `{read_and_passed_to, returned_by}`  
  → `data_reads`

#### 5.3.3 出边提取（Then）

- `METHOD -> STATE` 且 label 包含 `transitions_to`  
  → `post_state_effects`
- `METHOD -> DATA` 且 label in `{writes, allocates}`  
  → `data_writes`
- `METHOD -> METHOD`  
  → `downstream_calls`

并对 `downstream_calls` 去重保序。

---

### 5.4 意图构造（`intent_builder.py`）

规则驱动，不依赖 LLM。

#### 5.4.1 Functional（必生成）

- Given：前置状态 + 前序调用 + 读依赖
- When：`Class.method(...)` + 调用位置（是否有前驱）
- Then：状态变化 + 数据写入 + 下游传播
- 若 Then 全空，兜底断言：`return value satisfies the expected contract`

#### 5.4.2 Boundary/Exception（必生成）

- Given：若有前置状态则构造“违背前置状态”的描述；否则走通用边界输入说明
- When：null / empty / 0 / -1 / MAX 等边界输入
- Then：根据 `focal_code` 中关键词：
  - 含 `throw` / `Exception` → 期待异常
  - 含 `== null` / `null ==` → 期待 null 返回
  - 否则给通用容错预期

#### 5.4.3 Interaction/Dependency（条件生成）

仅当满足以下任一条件时生成：

- `post_state_effects` 非空
- `downstream_calls` 非空

Then 强调：
- 状态转换应发生
- 下游方法应观察到更新
- 相关写入数据应更新

---

### 5.5 上下文代码补全（`code_resolver.py`）

目的：让 Step 4 无需回查源码，直接从 `intents.json` 生成测试。

#### 5.5.1 方法代码来源策略

1. `pairs_index`：优先复用 Step 1 已抽取的 focal 代码（快且准确）
2. `source_scan`：扫描 `src/main/java/**/*.java`，按方法名做 brace-balanced 抽取

#### 5.5.2 字段定义提取

针对 `data_reads + data_writes` 中的变量名，在 focal 文件中匹配成员变量声明行：

- `private int port = ...;`
- `protected int maxThreads = ...;`

#### 5.5.3 imports 提取

保留 `package` + `import` 块，供后续测试生成时补齐类型上下文。

---

### 5.6 Step 3 主流程（`generator.py`）

对每个 pair：

1. `extractor.extract(...)` 得到 `slice`
2. `resolver.resolve_context(...)` 得到 `context_code`
3. `builder.build(slice, focal_code)` 生成 2~3 条意图
4. 打包 `IntentRecord` 并保存到 `intents.json`

---

## 6. Step 4：Test Generation（实现细节）

### 6.1 总体目标

输入：

- `intents.json`（Step 3 产出的结构化意图和上下文代码）
- `pairs.json`（Step 1 产出的历史用例对，用于提取 Few-Shot Example）

输出：

- `generated_tests.json`（包含最终生成的 JUnit 测试代码和所使用的完整 Prompt）

### 6.2 相似测试检索 (Few-Shot Examples)

为引导 LLM 学习目标项目的测试规范与风格，我们会在 Prompt 中加入 1-2 个该项目已有的测试用例。
核心算法 (`compute_similarity` & `get_similar_tests`)：
- **代码文本相似度 (Weight: 0.5)**：比较当前 Focal Method 与候选 Focal Method 源码的相似度（基于 `difflib.SequenceMatcher`）。
- **意图相似度 (Weight: 0.5)**：计算当前方法的意图集合与候选方法意图集合的 Jaccard 相似度（交集/并集）。
- 按加权总分降序排列，取 Top-2 作为 Few-Shot 示例。

### 6.3 Prompt 构造 (`prompt_builder.py`)

生成的 Prompt 包含五个结构化区块：
1. **系统角色设定**：声明大模型作为测试工程师，必须遵循 GWT 结构。
2. **代码上下文 (Code Context)**：注入 Step 3 解析好的 Imports、成员变量定义、Focal Method 源码以及关联方法源码。
3. **样例参考 (Similar Test Cases)**：注入上述检索到的 Few-Shot 代码。
4. **结构化测试意图 (Test Intents)**：将 JSON 格式的 `[Given]` (前置状态/数据), `[When]` (触发行为), `[Then]` (预期副作用) 转化为 Markdown 列表，强制 LLM 为每种意图（Functional, Boundary 等）生成独立的测试方法。
5. **输出约束 (Output Requirements)**：限制仅输出纯净的 `java` 代码块。

### 6.4 LLM 交互

- 目前集成使用的是 **DeepSeek API** (`deepseek-chat` 模型)。
- 使用低 Temperature (0.2) 以确保生成的代码逻辑严谨。
- 内置基于 HTTP 的错误重试与限流 (429) 回避机制。

---

## 7. 当前产物与质量状态

基于 `spark-master` 当前结果：

- 记录数：189（与 pairs 一致）
- 意图总数：416
  - Functional: 189
  - Boundary/Exception: 189
  - Interaction/Dependency: 38
- 测试生成率：100% (通过 Step 4 已成功对接 LLM 生成代码结构)

---

## 8. 维护建议（关键）

### 7.1 代码契约稳定性

若修改以下字段名，必须同步更新：
- `models.py` 的 `to_dict()`
- `generator.py` 序列化逻辑
- 下游 Step 4 的 JSON 读取器

建议新增 schema 版本号，例如：

```json
"schema_version": "step3.v2"
```

### 7.2 提取质量可提升点

1. `Step1._guess_focal_method` 增加更强匹配（LCS/调用图辅助）
2. `code_resolver` 对构造器 `<init>`、内部类方法增强解析
3. `field_definitions` 支持跨类字段溯源（目前偏 focal 文件内）
4. `when.parameters` 由占位改为 AST 解析出的真实签名

### 7.3 回归验证建议

每次改动后至少执行：

- `python run_pipeline.py --steps 3`
- 检查：
  - `records == len(pairs.json)`
  - 每条 record 至少 2 intents
  - `context_code.focal_code` 非空率接近 100%

---

## 9. 运行方式（复现）

```bash
python run_pipeline.py --steps 1
python run_pipeline.py --steps 2
python run_pipeline.py --steps 3
python run_pipeline.py --steps 4
```

或一次跑通全流程：

```bash
python run_pipeline.py --steps 1234
```

调试生成（仅对前 N 个意图生成测试，避免消耗过多 API 额度）：
```bash
python run_pipeline.py --steps 4 --limit 5
```

---

## 10. FAQ（简版）

**Q: 为什么很多记录没有 state precondition?**  
A: 当前 ESG 的 `guarded_by_*` 边本身较稀疏，属于图数据覆盖限制，不完全是提取器问题。

**Q: 为什么去掉 test_code?**  
A: 减少对 Step 4 的“旧测试污染”，让生成更贴近意图驱动而非模板改写。

**Q: Step 3 是否依赖 LLM?**  
A: 否。当前完全规则驱动，可复现、可调试。