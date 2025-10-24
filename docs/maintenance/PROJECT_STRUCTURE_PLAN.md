# 项目结构整理方案

## 当前问题分析

### 1. 根目录文件混乱
- 多个.md和.docx文档文件散落在根目录
- 工具脚本（embed_to_qdrant.py）应归入scripts目录
- 数据文件（MarketReg_QA.jsonl）体积巨大，位于根目录
- 修复文档应归入docs目录

### 2. 文档重复
- `智能自适应题库系统技术方案.md` 与 `docs/1.智能自适应题库系统技术方案.md` 重复
- `README_技术方案文档说明.md` 与 `docs/2.README_技术方案文档说明.md` 重复

### 3. 目录结构建议

```
06_QA_benchmark_MarketReg_2/
├── README.md                          # 项目主README
├── requirements.txt                   # 依赖配置
├── app.py                            # 主程序（保持在根目录）
├── adaptive_progress.json            # 运行时进度文件
│
├── adaptive/                         # 自适应学习核心模块
│   ├── __init__.py
│   ├── ability.py
│   ├── config.py
│   ├── models.py
│   ├── scheduler.py
│   ├── scorer.py
│   ├── selector.py
│   └── state_io.py
│
├── data/                             # 数据目录
│   ├── MarketReg_QA.jsonl           # 主数据集
│   ├── MarketReg_QA_ch.jsonl        # 中文数据集
│   ├── alimt_cache.json             # 翻译缓存
│   ├── history/                      # 学习历史记录
│   │   ├── rounds.jsonl
│   │   └── rounds/
│   ├── splits_marketreg_qa/         # 数据集分片
│   ├── splits_marketreg_qa_docx/    # 数据集文档
│   └── 翻译后的数据集/
│
├── scripts/                          # 工具脚本
│   ├── embed_to_qdrant.py           # 向量嵌入脚本（从根目录移入）
│   ├── batch_*.py                    # 批处理脚本
│   ├── translate_*.py                # 翻译脚本
│   └── ...
│
├── docs/                             # 文档目录
│   ├── README.md                     # 文档索引
│   ├── technical/                    # 技术文档（新建）
│   │   ├── 智能自适应题库系统技术方案.md
│   │   ├── 系统架构图.pdf
│   │   └── 系统架构图.png
│   ├── design/                       # 设计文档（新建）
│   │   ├── ADAPTIVE_LEARNING_FLOW.md
│   │   ├── UI_DESIGN_REVIEW.md
│   │   └── ...
│   ├── maintenance/                  # 维护文档（新建）
│   │   ├── FILTER_FIX_SUMMARY.md
│   │   ├── FILTER_DUPLICATE_FIX.md
│   │   └── PROJECT_CLEANUP_SUMMARY.md
│   └── presentations/                # 演示文档（新建）
│       ├── 电网知识自适应学习系统项目方案汇报v3.0.docx
│       └── 智能自适应题库系统技术方案.docx
│
└── archive/                          # 归档文件
    └── (保持不变)

```

## 整理操作清单

### 第一步：移动数据文件
- [x] MarketReg_QA.jsonl → data/
- [x] MarketReg_QA_ch.jsonl → data/ (如果存在)

### 第二步：移动脚本
- [x] embed_to_qdrant.py → scripts/

### 第三步：整理文档
- [x] 在docs下创建子目录：technical/, design/, maintenance/, presentations/
- [x] 移动技术文档到 docs/technical/
- [x] 移动设计文档到 docs/design/
- [x] 移动维护文档到 docs/maintenance/
- [x] 移动演示文档到 docs/presentations/
- [x] 删除根目录下的重复文档

### 第四步：更新README
- [x] 更新主README，添加清晰的项目结构说明
- [x] 更新docs/README.md，添加文档导航
