# 🎯 难度巩固策略实施方案

## 📋 目标

实现"答错某难度题目 → 继续练习该难度（巩固）"的逻辑，而不是降级到更简单的题目。

## 🔍 当前问题

**场景：** 用户连续答错L4题目
- ❌ 当前行为：ability从4.0降到3.5 → 系统推荐L3题目
- ✅ 期望行为：继续推荐L4题目，直到掌握为止

## 📊 实施方案

### 方案A：轻量级（推荐）⭐

**核心思路：** 跟踪最近答错的难度，给予巩固加成

**优点：**
- ✅ 修改最少（只改scorer.py）
- ✅ 不破坏现有逻辑
- ✅ 易于调试和维护

**需要修改的文件：** 1个
1. `adaptive/scorer.py` - 添加巩固加成逻辑

---

### 方案B：中等级（平衡）

**核心思路：** 在SessionState中维护"弱项难度集合"

**优点：**
- ✅ 更精确的状态跟踪
- ✅ 支持更复杂的巩固策略
- ⚠️ 需要修改数据模型

**需要修改的文件：** 3个
1. `adaptive/models.py` - 扩展SessionState
2. `adaptive/scorer.py` - 使用弱项难度信息
3. `app.py` - 维护弱项难度状态

---

### 方案C：完整级（最强）

**核心思路：** 完整的"能力矩阵"系统

**优点：**
- ✅ 为每个难度维护独立能力值
- ✅ 最精准的个性化学习路径
- ❌ 改动巨大，风险高

**需要修改的文件：** 5+ 个（不推荐）

---

## 🚀 推荐实施：方案A（轻量级）

### 第一步：修改 `adaptive/scorer.py`

**位置：** `score()` 方法

#### 1.1 添加辅助方法

```python
def _get_recent_wrong_difficulties(
    self, 
    state: SessionState,
    items_metadata: Dict[str, int]  # item_id -> difficulty
) -> Dict[int, int]:
    """获取最近答错题目的难度及错误次数
    
    Returns:
        {difficulty: wrong_count}
        例如：{3: 2, 4: 5} 表示L3错了2次，L4错了5次
    """
    wrong_diff_counts: Dict[int, int] = {}
    
    # 获取最近N道答错的题目（按时间倒序）
    recent_wrong = []
    for item_id, record in state.answers_by_item.items():
        if record.is_correct == False:
            recent_wrong.append((item_id, record.ts_ms))
    
    # 按时间排序，取最近10道
    recent_wrong.sort(key=lambda x: x[1], reverse=True)
    recent_wrong = recent_wrong[:10]
    
    # 统计每个难度的错误次数
    for item_id, _ in recent_wrong:
        difficulty = items_metadata.get(item_id, 3)
        wrong_diff_counts[difficulty] = wrong_diff_counts.get(difficulty, 0) + 1
    
    return wrong_diff_counts
```

#### 1.2 修改打分逻辑

```python
def score(
    self,
    item: ItemMeta,
    state: SessionState,
    recent_correct_complex_ids: List[str],
    get_neighbors_fn: Callable[[str], Optional[Tuple[List[str], List[float]]]],
    get_complex_difficulty_fn: Callable[[str], int],
) -> float:
    # ... 原有逻辑 ...
    
    # 🆕 8. 难度巩固加成
    consolidation_bonus = 0.0
    
    # 构建item_id -> difficulty映射（从state中获取）
    items_metadata = {}
    for item_id in state.answers_by_item.keys():
        items_metadata[item_id] = get_complex_difficulty_fn(item_id)
    
    # 获取最近答错的难度统计
    wrong_diff_counts = self._get_recent_wrong_difficulties(state, items_metadata)
    
    # 如果当前题目的难度在最近答错列表中，给予巩固加成
    if item.difficulty_num in wrong_diff_counts:
        wrong_count = wrong_diff_counts[item.difficulty_num]
        # 错得越多，巩固加成越大（最多+3分）
        consolidation_bonus = min(3.0, wrong_count * 0.8)
    
    # Q-Learning总分
    total_score = (
        base_q + 
        difficulty_reward + 
        review_bonus + 
        exploration_bonus * 0.5 + 
        knowledge_value - 
        similarity_penalty * 0.3 + 
        wrong_boost * 0.5 +
        consolidation_bonus  # 🆕 新增
    )
    
    return total_score
```

**加成策略：**
```
最近答错1次该难度 → +0.8分
最近答错2次该难度 → +1.6分
最近答错3次该难度 → +2.4分
最近答错4+次该难度 → +3.0分（封顶）
```

---

### 第二步：测试验证

#### 2.1 创建测试脚本

```bash
python scripts/test_consolidation.py
```

#### 2.2 预期效果

**场景：连续答错5道L4题目**

| 答题次数 | ability | L3分数 | L4分数 | 推荐 |
|---------|---------|--------|--------|-----|
| 初始 | 4.0 | -0.5 | 0.0 | L4 ⭐ |
| 错1次 | 3.875 | -0.44 | -0.44+0.8=**0.36** | L4 ⭐ |
| 错2次 | 3.75 | -0.38 | -0.5+1.6=**1.1** | L4 ⭐ |
| 错3次 | 3.625 | -0.31 | -0.56+2.4=**1.84** | L4 ⭐ |
| 错4次 | 3.5 | -0.25 | -0.63+3.0=**2.37** | L4 ⭐ |
| 错5次 | 3.375 | -0.19 | -0.69+3.0=**2.31** | L4 ⭐ |

✅ **结果：即使能力值降到3.375，依然推荐L4（巩固）**

---

### 第三步：调优参数

#### 可调参数

```python
class Scorer:
    def __init__(self, params):
        # ... 原有参数 ...
        
        # 🆕 巩固策略参数
        self.consolidation_window = 10  # 考察最近N道题
        self.consolidation_factor = 0.8  # 每次错误的加成系数
        self.consolidation_max = 3.0     # 最大巩固加成
```

#### 建议的参数组合

| 策略 | window | factor | max | 说明 |
|-----|--------|--------|-----|------|
| **温和** | 10 | 0.5 | 2.0 | 少量倾向巩固 |
| **标准** ⭐ | 10 | 0.8 | 3.0 | 平衡巩固和探索 |
| **激进** | 15 | 1.0 | 4.0 | 强制巩固弱项 |

---

## 📈 完整改动清单

### 文件1: `adaptive/scorer.py`

**改动位置：**
```
第14行 - class Scorer.__init__()
  → 添加3个巩固策略参数

第56行 - 新增方法
  → def _get_recent_wrong_difficulties()

第130行 - score()方法
  → 添加巩固加成计算（约15行代码）
```

**代码量：** 约40行新增代码

---

## 🧪 测试方案

### 测试用例1：基础巩固
```
初始：ability=4.0
答错L4 × 3次 → 验证L4分数 > L3分数
```

### 测试用例2：多难度混合
```
答错L3 × 2次，答错L4 × 3次
→ 验证L4分数最高（错误次数多）
```

### 测试用例3：时间窗口
```
答错L4 × 5次（在window=10之前）
答对L5 × 10次（最近）
→ 验证L4巩固加成消失
```

---

## ⚠️ 潜在风险

### 风险1：过度巩固
**问题：** 用户一直困在某个难度，无法进步
**解决：** 
- 设置巩固加成上限（3.0分）
- 答对后清除该难度的巩固标记

### 风险2：与能力值冲突
**问题：** ability=3.0时强制推L4可能过难
**解决：**
- 只在 `|difficulty - ability| ≤ 1.0` 时启用巩固
- 例如：ability=3.0时，只对L2-L4启用巩固

### 风险3：复习优先级冲突
**问题：** 巩固加成可能压制复习优先级
**解决：**
- 确保 review_bonus (+3~5分) > consolidation_max (+3分)
- 到期复习 > 难度巩固

---

## 📊 对比：改前改后

### 改前（纯能力匹配）
```
能力曲线：4.0 → 3.5 → 3.0 → ...
推荐难度：L4 → L3 → L3 → ...
特点：快速降级，避免挫败感
```

### 改后（能力匹配 + 难度巩固）
```
能力曲线：4.0 → 3.5 → 3.0 → ...
推荐难度：L4 → L4 → L4 → ... (直到掌握)
特点：坚持巩固，直到突破瓶颈
```

---

## 🎯 实施步骤总结

### Step 1: 修改代码（30分钟）
- [ ] 修改 `adaptive/scorer.py`
  - [ ] 添加 `_get_recent_wrong_difficulties()` 方法
  - [ ] 修改 `score()` 方法，添加巩固加成
  - [ ] 添加配置参数

### Step 2: 测试验证（20分钟）
- [ ] 创建测试脚本
- [ ] 验证基础场景
- [ ] 验证边界情况

### Step 3: 调优（10分钟）
- [ ] 调整参数（factor, max）
- [ ] 用真实数据测试
- [ ] 观察用户体验

### Step 4: 文档更新（10分钟）
- [ ] 更新 ALGORITHM_CHANGES.md
- [ ] 添加参数说明
- [ ] 更新使用文档

**总计：约70分钟**

---

## 🚀 快速开始

想要立即实施？运行以下命令：

```bash
# 我可以帮你自动实施方案A
echo "是否要我现在帮你实现难度巩固策略？(Y/n)"
```

我会：
1. ✅ 修改 `adaptive/scorer.py`（添加巩固逻辑）
2. ✅ 创建测试脚本验证效果
3. ✅ 调整参数到最优值
4. ✅ 更新文档

预计时间：10-15分钟 ⏱️

