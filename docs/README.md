# 项目文档导航

本目录包含智能自适应题库系统的所有文档，按类型分类组织。

## 📂 文档目录结构

```
docs/
├── technical/        # 技术文档
├── design/          # 设计文档
├── maintenance/     # 维护文档
└── presentations/   # 演示文档
```

---

## 📘 技术文档 (technical/)

系统架构、技术方案和技术实现相关文档。

### 核心文档
- **[智能自适应题库系统技术方案.md](technical/智能自适应题库系统技术方案.md)**  
  完整的技术方案文档，包含系统架构、算法设计、技术选型等

- **[系统架构图.pdf](technical/系统架构图.pdf)** / **[.png](technical/系统架构图.png)**  
  系统架构可视化图表

- **[算法体系演进对比分析.md](technical/3.算法体系演进对比分析.md)**  
  自适应学习算法的演进历程和对比分析

### 其他技术文档
- **[1.智能自适应题库系统技术方案.md](technical/1.智能自适应题库系统技术方案.md)**  
  早期版本的技术方案

- **[2.README_技术方案文档说明.md](technical/2.README_技术方案文档说明.md)**  
  技术方案文档的使用说明

---

## 🎨 设计文档 (design/)

UI/UX设计、功能设计和产品设计相关文档。

### 核心流程
- **[ADAPTIVE_LEARNING_FLOW.md](design/ADAPTIVE_LEARNING_FLOW.md)**  
  自适应学习流程的详细说明（1059行大型文档）

### UI设计
- **[UI_DESIGN_REVIEW.md](design/UI_DESIGN_REVIEW.md)**  
  UI设计评审和建议

- **[UI_OPTIMIZATION_SUMMARY.md](design/UI_OPTIMIZATION_SUMMARY.md)**  
  UI优化总结

- **[UI_REDESIGN_SUMMARY.md](design/UI_REDESIGN_SUMMARY.md)**  
  UI重设计总结

- **[COMPACT_LAYOUT_CHANGES.md](design/COMPACT_LAYOUT_CHANGES.md)**  
  紧凑布局的改进

### 功能设计
- **[QUESTION_OPTIONS_ENHANCEMENT.md](design/QUESTION_OPTIONS_ENHANCEMENT.md)**  
  题目选项功能增强

- **[LEARNING_RECORDS_OPTIMIZATION.md](design/LEARNING_RECORDS_OPTIMIZATION.md)**  
  学习记录优化

- **[DESIGN_COMPARISON.md](design/DESIGN_COMPARISON.md)**  
  设计方案对比

### 算法设计
- **[ALGORITHM_CHANGES.md](design/ALGORITHM_CHANGES.md)**  
  算法变更记录

- **[CONSOLIDATION_STRATEGY_PLAN.md](design/CONSOLIDATION_STRATEGY_PLAN.md)**  
  整合策略规划

---

## 🔧 维护文档 (maintenance/)

问题修复、系统维护和更新日志。

### 问题修复记录
- **[FILTER_FIX_SUMMARY.md](maintenance/FILTER_FIX_SUMMARY.md)**  
  筛选功能修复详细记录（包含两次修复的完整历史）

- **[FILTER_DUPLICATE_FIX.md](maintenance/FILTER_DUPLICATE_FIX.md)**  
  筛选框重复选项修复报告（可视化文档）

- **[PROJECT_CLEANUP_SUMMARY.md](maintenance/PROJECT_CLEANUP_SUMMARY.md)**  
  项目清理总结

### 最近修复（2024-10-24）
1. ✅ 修复筛选功能无效问题
   - 原因：数据集类型名称未正确映射
   - 影响：用户无法使用题型筛选功能
   
2. ✅ 消除筛选框重复选项
   - 原因：12种类型变体未统一标准化
   - 解决：实现三步标准化流程（去后缀→标准化→模式匹配）

---

## 📊 演示文档 (presentations/)

用于展示和汇报的文档。

- **[智能自适应题库系统技术方案.docx](presentations/智能自适应题库系统技术方案.docx)**  
  技术方案演示文档（Word格式）

- **[电网知识自适应学习系统项目方案汇报v3.0.docx](presentations/电网知识自适应学习系统项目方案汇报v3.0.docx)**  
  项目方案汇报文档（PPT格式）

---

## 🔍 文档索引

### 按主题查找

#### 1. 了解系统架构
- [技术方案](technical/智能自适应题库系统技术方案.md)
- [系统架构图](technical/系统架构图.pdf)

#### 2. 理解自适应学习算法
- [自适应学习流程](design/ADAPTIVE_LEARNING_FLOW.md)
- [算法体系演进](technical/3.算法体系演进对比分析.md)
- [算法变更记录](design/ALGORITHM_CHANGES.md)

#### 3. UI/UX设计
- [UI设计评审](design/UI_DESIGN_REVIEW.md)
- [UI优化总结](design/UI_OPTIMIZATION_SUMMARY.md)
- [布局改进](design/COMPACT_LAYOUT_CHANGES.md)

#### 4. 问题排查
- [筛选功能修复](maintenance/FILTER_FIX_SUMMARY.md)
- [重复选项修复](maintenance/FILTER_DUPLICATE_FIX.md)

#### 5. 功能增强
- [题目选项增强](design/QUESTION_OPTIONS_ENHANCEMENT.md)
- [学习记录优化](design/LEARNING_RECORDS_OPTIMIZATION.md)

---

## 📝 文档更新日志

### 2024-10-24
- ✅ 重组文档目录结构
- ✅ 创建技术/设计/维护/演示四大分类
- ✅ 添加本导航文档

---

## 💡 贡献指南

### 添加新文档
根据文档类型，将新文档放入相应目录：
- **技术实现** → `technical/`
- **设计方案** → `design/`
- **问题修复** → `maintenance/`
- **演示材料** → `presentations/`

### 文档命名规范
- 使用清晰、描述性的文件名
- 英文文档使用大写单词+下划线（如`UI_DESIGN_REVIEW.md`）
- 中文文档使用全中文（如`系统架构图.pdf`）
- 避免使用特殊字符

---

**最后更新**: 2024-10-24
