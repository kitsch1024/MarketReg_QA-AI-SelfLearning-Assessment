# 智能自适应题库系统 (Adaptive Learning System)

一个基于自适应学习算法的智能题库系统，专注于市场监管知识领域的学习和评估。

## 🌟 核心特性

- **自适应学习算法**：基于IRT（项目反应理论）动态调整题目难度
- **智能选题机制**：结合向量检索和能力评估的智能选题
- **学习记录追踪**：完整的学习历史记录和进度管理
- **多类型题目支持**：单选、多选、判断、填空等多种题型
- **优雅的UI设计**：现代化的用户界面，支持悬浮折叠侧边栏

## 📁 项目结构

```
06_QA_benchmark_MarketReg_2/
├── README.md                          # 项目说明文档
├── requirements.txt                   # Python依赖配置
├── app.py                            # 主应用程序（Streamlit）
├── adaptive_progress.json            # 学习进度文件（运行时生成）
│
├── adaptive/                         # 自适应学习核心模块
│   ├── __init__.py                   # 模块初始化
│   ├── ability.py                    # 能力评估模块
│   ├── config.py                     # 配置管理
│   ├── models.py                     # 数据模型定义
│   ├── scheduler.py                  # 学习调度器
│   ├── scorer.py                     # 评分器
│   ├── selector.py                   # 智能选题器
│   └── state_io.py                   # 状态IO管理
│
├── data/                             # 数据目录
│   ├── MarketReg_QA.jsonl           # 主数据集（市场监管题库）
│   ├── alimt_cache.json             # 翻译缓存
│   ├── history/                      # 学习历史记录
│   │   ├── rounds.jsonl             # 轮次汇总
│   │   └── rounds/                   # 各轮次详细记录
│   ├── splits_marketreg_qa/         # 数据集分片（JSONL格式）
│   ├── splits_marketreg_qa_docx/    # 数据集分片（DOCX格式）
│   └── 翻译后的数据集/               # 翻译后的数据文件
│
├── scripts/                          # 工具脚本目录
│   ├── embed_to_qdrant.py           # 向量嵌入脚本
│   ├── batch_*.py                    # 批处理脚本
│   ├── translate_*.py                # 翻译相关脚本
│   ├── split_*.py                    # 数据集分割脚本
│   └── ...                           # 其他工具脚本
│
├── docs/                             # 文档目录
│   ├── README.md                     # 文档索引
│   ├── technical/                    # 技术文档
│   │   ├── 智能自适应题库系统技术方案.md
│   │   ├── 系统架构图.pdf
│   │   └── ...
│   ├── design/                       # 设计文档
│   │   ├── ADAPTIVE_LEARNING_FLOW.md
│   │   ├── UI_DESIGN_REVIEW.md
│   │   └── ...
│   ├── maintenance/                  # 维护文档
│   │   ├── FILTER_FIX_SUMMARY.md
│   │   ├── FILTER_DUPLICATE_FIX.md
│   │   └── ...
│   └── presentations/                # 演示文档
│       └── ...
│
└── archive/                          # 归档文件
    └── (旧版本代码)

```

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 运行应用

```bash
streamlit run app.py
```

### 3. 使用系统

1. 应用启动后会自动打开浏览器
2. 右侧悬浮栏可以选择题目类型、难度等筛选条件
3. 点击"Start Session"开始学习
4. 系统会根据你的答题情况动态调整题目难度

## 📊 数据集

主数据集 `data/MarketReg_QA.jsonl` 包含22,853道市场监管相关题目，涵盖：

- **题型**：单选题、多选题、判断题、填空题
- **难度**：L1-L5 五个难度级别
- **领域**：行政法、社会法、环境法等多个法律领域
- **来源**：标准化的法律知识题库

## 🔧 核心模块说明

### adaptive/ - 自适应学习核心

- **ability.py**: 能力评估算法，基于IRT理论
- **selector.py**: 智能选题器，结合向量检索和能力匹配
- **scheduler.py**: 学习调度器，管理复习间隔
- **scorer.py**: 评分器，处理答案匹配和评分逻辑

### scripts/ - 工具脚本

- **embed_to_qdrant.py**: 将题目向量化并存储到Qdrant向量数据库
- **translate_*.py**: 数据集翻译相关脚本
- **split_*.py**: 数据集分割和处理脚本

## 📖 文档导航

- **技术方案**: [docs/technical/智能自适应题库系统技术方案.md](docs/technical/智能自适应题库系统技术方案.md)
- **系统架构**: [docs/technical/系统架构图.pdf](docs/technical/系统架构图.pdf)
- **自适应学习流程**: [docs/design/ADAPTIVE_LEARNING_FLOW.md](docs/design/ADAPTIVE_LEARNING_FLOW.md)
- **维护日志**: [docs/maintenance/](docs/maintenance/)

## 🛠️ 技术栈

- **前端框架**: Streamlit
- **核心算法**: IRT (Item Response Theory)
- **向量检索**: Qdrant
- **数据处理**: Python 3.x
- **持久化**: JSON/JSONL

## 📝 开发者注意事项

### 配置文件
- `adaptive_progress.json`: 存储学习进度，应用运行时自动生成

### 数据路径
- 主数据集默认路径: `data/MarketReg_QA.jsonl`
- 历史记录路径: `data/history/`

### 关键依赖
- streamlit
- qdrant-client（可选，用于向量检索）
- pandas（用于数据分析）

## 🐛 问题修复历史

最近的重要修复：
- ✅ 修复筛选功能无效问题（2025-10-24）
- ✅ 消除筛选框中的重复选项（2025-10-24）
- ✅ 优化项目文件结构（2025-10-24）

详见 [docs/maintenance/](docs/maintenance/) 目录。

## 📄 许可证

本项目仅供学习和研究使用。

## 👥 贡献

欢迎提交Issue和Pull Request！

---

**最后更新**: 2025-10-24
