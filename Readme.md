# Intent-Driven Testing (基于意图的测试生成)

> 解决 LLM 生成测试时缺乏项目特定执行语义（如生命周期、状态依赖）的问题。

## 🎯 项目核心目标
通过构建 **执行语义图 (ESG)**，提取显式的 **测试意图 (Intent)**，结合 LLM 生成高质量、符合语义约束且可执行的 JUnit 测试用例。

---

## 🚀 核心流程 (Pipeline)

项目包含 5 个核心步骤，目前已 **全流程跑通**：

1.  **Input Transformation**: 扫描 Java 源码，提取测试与被测方法对 (`pairs.json`)。
2.  **ESG Construction**: 编译并分析代码，构建执行语义图 (`esg_graph.json`)。
3.  **Intent Generation**: 基于 ESG 图谱提取结构化测试意图 (`[Given-When-Then]`)。
4.  **Test Generation**: 
    - 动态解析 `pom.xml` 依赖（自动适配 JUnit 4/5）。
    - 结合意图、上下文代码和相似用例 (Few-Shot)，调用 LLM 生成测试代码。
5.  **Execution & Self-Correction**: 
    - **项目级环境隔离**：并发执行测试，避免 Maven 冲突。
    - **自我修复循环**：自动捕获编译/运行错误，反馈给 LLM 进行最多 3 次修复。

---

## 📊 当前进度 (Status)

| 步骤 | 状态 | 关键产物 | 说明 |
| :--- | :--- | :--- | :--- |
| **Step 1** | ✅ 完成 | `pairs.json` | 提取了 189 对被测方法 |
| **Step 2** | ✅ 完成 | `esg_graph.json` | 构建了 1615 个节点的语义图 |
| **Step 3** | ✅ 完成 | `intents.json` | 生成了 416 条结构化意图 |
| **Step 4** | ✅ 完成 | `generated_tests.json` | 成功对接 DeepSeek API 生成代码 |
| **Step 5** | ✅ 完成 | `execution_results.json` | **闭环跑通**：已验证自动修复机制有效 |

---

## 🛠️ 快速开始

**前置要求**: Python 3.8+, Maven, Java 8+

1.  **配置 API Key**:
    在 `.env` 文件中设置 `DEEPSEEK_API_KEY`。

2.  **运行全流程**:
    ```bash
    # 一键执行 Step 1 到 5
    python run_pipeline.py --steps 12345
    ```

3.  **调试运行**:
    ```bash
    # 仅运行生成和执行步骤（前 5 个用例）
    python run_pipeline.py --steps 45 --limit 5
    ```

---

## 📂 目录结构

*   `pipeline/`: 核心 Python 脚本
    *   `step4_test_generation/`: Prompt 构造与 LLM 调用
    *   `step5_test_execution/`: Maven 执行与修复循环
*   `esg_construction/`: Java 静态分析器源码
*   `data/`: 
    *   `raw/`: 原始项目源码 (如 `spark-master`)
    *   `processed/`: 中间产物与最终结果
