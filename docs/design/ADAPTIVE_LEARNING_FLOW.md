# 自适应学习系统完整实现路径

## 📋 目录
1. [系统架构概览](#系统架构概览)
2. [完整学习流程](#完整学习流程)
3. [核心算法详解](#核心算法详解)
4. [代码路径映射](#代码路径映射)
5. [数据流转图](#数据流转图)

---

## 🏗️ 系统架构概览

### 模块组成
```
MarketReg_QA/
├── app.py                      # 主应用（UI + 业务逻辑）
├── adaptive/                   # 自适应学习引擎
│   ├── config.py              # 配置参数
│   ├── models.py              # 数据模型
│   ├── scheduler.py           # Leitner间隔重复调度器
│   ├── scorer.py              # 智能打分系统
│   ├── selector.py            # Softmax选题器
│   └── state_io.py            # 状态持久化
├── data/
│   ├── history/               # 学习历史记录
│   │   ├── rounds.jsonl       # 轮次摘要
│   │   └── rounds/*.json      # 轮次详情
│   └── adaptive_progress.json # 当前进度
└── MarketReg_QA.jsonl         # 题库数据
```

---

## 🔄 完整学习流程

### 阶段一：系统初始化（启动会话）

#### 1.1 导入自适应模块
**位置：** `app.py` 第36-51行
```python
try:
    from adaptive.config import QdrantConfig, AdaptiveParams
    from adaptive.scorer import Scorer
    from adaptive.selector import Selector
    from adaptive.scheduler import Scheduler
    from qdrant_client import QdrantClient
except Exception:
    # 降级处理：模块不可用时使用简单排序
    Scorer = None
    Selector = None
    Scheduler = None
```

**说明：** 
- 如果自适应模块可用，使用高级选题算法
- 如果不可用，降级为基于难度的简单排序

---

#### 1.2 创建Selector实例
**位置：** `app.py` 第2023-2033行
```python
def _ensure_selector() -> Optional["Selector"]:
    if Selector is None or Scorer is None or AdaptiveParams is None:
        return None
    if "_selector" in st.session_state:
        return st.session_state["_selector"]
    
    # 创建配置参数
    params = AdaptiveParams()
    # 创建打分器
    scorer = Scorer(params)
    # 创建选择器（temperature=0.5）
    selector = Selector(scorer, temp=params.softmax_temperature)
    
    st.session_state["_selector"] = selector
    st.session_state["_adaptive_params"] = params
    return selector
```

**核心参数（AdaptiveParams）：**
```python
sim_threshold: 0.70           # 相似度阈值
suppress_lambda: 6.0          # 相似题抑制系数
boost_lambda: 3.0             # 错题增强系数
ability_init: 1.0             # 初始能力值
ability_step_correct: +0.15   # 答对能力增量
ability_step_wrong: -0.15     # 答错能力减量
softmax_temperature: 0.5      # 采样温度
review_intervals_days: (1,3,7,21)  # 复习间隔（天）
```

---

#### 1.3 初始化会话状态
**位置：** `app.py` 第3094-3197行（`start_btn` 点击后）
```python
# 初始能力值
ability = 1.0

# 初始化状态变量
st.session_state.items = items_sorted           # 排序后的题目列表
st.session_state.item_idx = 0                   # 当前题目索引
st.session_state.correct_count = 0              # 答对数量
st.session_state.answered_count = 0             # 已答数量
st.session_state.ability = ability              # 能力值
st.session_state.answered_items = set()         # 已答题目ID集合
st.session_state.answers_by_item = {}           # 答题记录字典
st.session_state.recent_correct_complex_ids = [] # 最近答对的复杂题ID
st.session_state.review_schedule = {}           # 复习计划
```

---

### 阶段二：智能选题（首次加载题目）

#### 2.1 加载并筛选题目
**位置：** `app.py` 第2051-2095行
```python
def load_filtered_items(
    file_path: str,
    selected_fields: Set[str],
    selected_types: Set[str],
    selected_difficulties: Set[str],
    max_items: int = 50
) -> List[Item]:
    items = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                # 应用筛选条件
                if selected_fields and obj.get("field") not in selected_fields:
                    continue
                if selected_types and obj.get("type") not in selected_types:
                    continue
                if selected_difficulties and obj.get("difficulty") not in selected_difficulties:
                    continue
                items.append(obj)
                if len(items) >= max_items:
                    break
            except Exception:
                continue
    return items
```

**说明：** 根据用户在侧边栏选择的筛选条件加载题目

---

#### 2.2 智能选题（Selector + Scorer）
**位置：** `app.py` 第3097-3175行

##### 步骤1：去重（基于UID）
```python
seen_uids: Set[str] = set()
dedup_items = []
for it in items:
    uid = get_item_uid(it)
    if uid not in seen_uids:
        seen_uids.add(uid)
        dedup_items.append(it)
```

##### 步骤2：转换为ItemMeta
```python
candidate_metas = []
for it in dedup_items:
    meta = ItemMeta(
        id=it.get("id", ""),
        difficulty_num=_parse_difficulty_num(it.get("difficulty")),
        knowledge_points=it.get("knowledge_points", [])
    )
    candidate_metas.append(meta)
```

##### 步骤3：创建状态适配器
```python
class _S:
    def __init__(self, ability):
        self.ability = ability              # 当前能力值
        self.review_schedule = {}           # 复习计划（空）
        self.kp_mastery = {}                # 知识点掌握度（空）
        self.answers_by_item = {}           # 答题记录（空）

state_adapter = _S(ability)
```

##### 步骤4：调用Selector.choose()
**位置：** `adaptive/selector.py` 第19-34行
```python
def choose(
    candidates: Iterable[ItemMeta],
    state: SessionState,
    recent_correct_complex_ids: List[str],
    get_neighbors_fn: Callable,
    get_complex_difficulty_fn: Callable,
    k: int = 20
) -> List[ItemMeta]:
    # 1. 为每个候选题打分
    scored: List[tuple[ItemMeta, float]] = []
    for it in candidates:
        s = self.scorer.score(
            it, 
            state, 
            recent_correct_complex_ids,
            get_neighbors_fn, 
            get_complex_difficulty_fn
        )
        scored.append((it, s))
    
    # 2. Softmax概率转换
    probs = self._softmax([s for _, s in scored], self.temp)
    
    # 3. 多项式采样（探索vs利用平衡）
    return self._multinomial(scored, probs, k)
```

**关键算法：Softmax采样**
```python
def _softmax(xs: List[float], temp: float) -> List[float]:
    # temp越小，选择越确定性（利用）
    # temp越大，选择越随机（探索）
    m = max(xs)
    exps = [math.exp((x - m) / temp) for x in xs]
    Z = sum(exps)
    return [e / Z for e in exps]
```

---

#### 2.3 打分算法详解
**位置：** `adaptive/scorer.py` 第18-52行

##### 打分公式
```python
Score = 难度适配分 + 复习优先分 + 知识点覆盖分
        - 相似题抑制惩罚 + 错题增强奖励
```

##### 2.3.1 难度适配分
```python
d = item.difficulty_num
s += -abs(d - state.ability)
```
**说明：** 
- 难度与能力值越接近，分数越高
- 例：ability=2.5, difficulty=3 → score += -0.5
- 例：ability=2.5, difficulty=1 → score += -1.5

##### 2.3.2 复习优先分
```python
entry = state.review_schedule.get(item.id)
if entry:
    now = int(time.time() * 1000)
    if now >= entry.next_ts_ms:  # 已到期
        overdue_days = (now - entry.next_ts_ms) / 86400000.0
        s += 2.0 + min(3.0, overdue_days)  # +2~5分
```
**说明：** 
- 到期题目基础+2分
- 过期越久，额外加分（最多+3分）
- 总计：+2~5分

##### 2.3.3 知识点覆盖分
```python
if item.knowledge_points:
    uncovered = [
        kp for kp in item.knowledge_points 
        if state.kp_mastery.get(kp, 0) < d
    ]
    if uncovered:
        s += 0.5
```
**说明：** 包含未掌握知识点的题目+0.5分

##### 2.3.4 相似题抑制惩罚
**位置：** `adaptive/scorer.py` 第54-78行
```python
def _similar_suppression(
    item: ItemMeta,
    complex_ids: List[str],  # 最近答对的复杂题ID
    get_neighbors_fn: Callable,
    get_complex_difficulty_fn: Callable
) -> float:
    penalty = 0.0
    for cid in complex_ids:
        # 获取复杂题的相似题列表
        nn = get_neighbors_fn(cid)
        if not nn:
            continue
        nn_ids, nn_sims = nn
        
        # 检查当前题是否在相似题列表中
        try:
            idx = nn_ids.index(item.id)
        except ValueError:
            continue
        
        sim = nn_sims[idx]
        if sim < self.p.sim_threshold:  # 0.70
            continue
        
        # 如果当前题比复杂题简单，则降权
        d_complex = max(3, get_complex_difficulty_fn(cid))
        if item.difficulty_num <= d_complex - 1:
            diff_delta = d_complex - item.difficulty_num
            penalty += self.p.suppress_lambda * sim * diff_delta
            # suppress_lambda=6.0
```

**举例：**
- 用户答对了难度=4的题A
- 题B难度=2，与A相似度=0.85
- 惩罚 = 6.0 × 0.85 × (4-2) = 10.2分
- 题B的总分会减少10.2分

**目的：** 防止用户重复做相似的简单题，浪费时间

##### 2.3.5 错题增强奖励
**位置：** `adaptive/scorer.py` 第80-115行
```python
def _wrong_boost(
    item: ItemMeta,
    state: SessionState,
    get_neighbors_fn: Callable
) -> float:
    # 获取最近10道错题
    wrong_pairs = [
        (qid, rec.ts_ms)
        for qid, rec in state.answers_by_item.items()
        if rec.is_correct is False
    ]
    wrong_pairs.sort(key=lambda x: x[1], reverse=True)
    wrong_ids = [qid for qid, _ in wrong_pairs[:10]]
    
    boost = 0.0
    for wid in wrong_ids:
        # 获取错题的相似题列表
        nn = get_neighbors_fn(wid)
        if not nn:
            continue
        nn_ids, nn_sims = nn
        
        # 检查当前题是否在相似题列表中
        try:
            idx = nn_ids.index(item.id)
        except ValueError:
            continue
        
        sim = nn_sims[idx]
        if sim <= 0:
            continue
        
        boost += self.p.boost_lambda * sim
        # boost_lambda=3.0
    
    return boost
```

**举例：**
- 用户答错了题A
- 题B与A相似度=0.80
- 奖励 = 3.0 × 0.80 = 2.4分
- 题B的总分增加2.4分

**目的：** 强化薄弱环节，让用户多练习类似的题目

---

#### 2.4 降级处理（无Selector时）
**位置：** `app.py` 第2098-2103行
```python
def sort_items_for_adaptive(items: List[Item], ability: float) -> List[Item]:
    def key_fn(it: Item) -> Tuple[float, str]:
        d = get_item_difficulty_score(it)
        return (abs(d - ability), str(it.get("id", "")))
    
    return sorted(items, key=key_fn)
```
**说明：** 按难度与能力值的距离排序（最近的优先）

---

### 阶段三：用户答题

#### 3.1 显示题目
**位置：** `app.py` 第2246-2600行
```python
def show_item_view(item: Item) -> Tuple[Optional[bool], Dict[str, Any]]:
    # 1. 渲染题目（question）
    # 2. 渲染选项（单选/多选/判断/填空/开放）
    # 3. 显示"Submit Answer"按钮
    # 4. 锁定已提交的答案（is_locked）
    # 5. 返回评估结果（is_correct）和用户输入
```

#### 3.2 评估答案
**位置：** `app.py` 第2246-2600行（各题型的评估逻辑）

**单选题：**
```python
def evaluate_single_choice(choice: str, correct_letter: str) -> bool:
    return choice == correct_letter
```

**多选题：**
```python
def evaluate_multiple_choice(
    selected_options: List[str], 
    correct_letters: List[str]
) -> bool:
    return set(selected_options) == set(correct_letters)
```

**判断题：**
```python
def evaluate_true_false(choice: str, correct_answer: str) -> bool:
    return choice.lower() == correct_answer.lower()
```

**填空题 & 开放题：** 人工评分（is_correct=None）

---

### 阶段四：提交后处理（核心自适应逻辑）

#### 4.1 更新统计数据
**位置：** `app.py` 第3536-3557行
```python
# 增加已答数量
st.session_state.answered_count += 1

# 如果答对
if is_correct:
    st.session_state.correct_count += 1
    
    # 能力值提升
    st.session_state.ability += 0.15
    
    # 记录答对的复杂题（用于相似题抑制）
    if _parse_difficulty_num(item.get("difficulty")) >= 3:
        arr = st.session_state.get("recent_correct_complex_ids", [])
        arr.append(str(item.get("id")))
        st.session_state["recent_correct_complex_ids"] = arr[-30:]
else:
    # 能力值下降
    st.session_state.ability -= 0.15

# 限制能力值范围 [1.0, 5.0]
st.session_state.ability = max(1.0, min(5.0, st.session_state.ability))
```

**能力值变化示例：**
```
初始: ability = 1.0
答对一题: ability = 1.15
再答对: ability = 1.30
答错一题: ability = 1.15
连续答对3题: ability = 1.15 + 0.15×3 = 1.60
```

---

#### 4.2 更新复习计划（Leitner算法）
**位置：** `app.py` 第3559-3575行
```python
if Scheduler is not None:
    from adaptive.scheduler import ReviewEntry
    
    # 获取间隔配置
    intervals = st.session_state.get("_scheduler_intervals", (1,3,7,21))
    sched = Scheduler(intervals)
    
    # 获取旧的复习条目
    rs = st.session_state.get("review_schedule", {})
    old = rs.get(current_item_id)
    if old:
        old_entry = ReviewEntry(
            bucket=int(old.get("bucket", 0)),
            next_ts_ms=int(old.get("next_ts_ms", 0))
        )
    else:
        old_entry = None
    
    # 根据答题结果更新复习计划
    new_entry = sched.on_result(old_entry, is_correct, now_ms)
    
    # 保存新的复习计划
    rs[current_item_id] = {
        "bucket": new_entry.bucket,
        "next_ts_ms": new_entry.next_ts_ms
    }
    st.session_state["review_schedule"] = rs
```

**Leitner算法详解：**
**位置：** `adaptive/scheduler.py` 第16-30行
```python
def on_result(
    entry: ReviewEntry | None, 
    is_correct: bool, 
    now_ms: int
) -> ReviewEntry:
    bucket = entry.bucket if entry else 0
    
    if is_correct:
        # 答对：桶升级
        bucket = min(bucket + 1, len(self.intervals_ms) - 1)
    else:
        # 答错：桶降级
        bucket = max(bucket - 1, 0)
    
    # 计算下次复习时间
    next_ts = now_ms + self.intervals_ms[bucket]
    return ReviewEntry(bucket=bucket, next_ts_ms=next_ts)
```

**间隔示例：** `(1, 3, 7, 21)` 天
```
初始: bucket=0, 1天后复习
答对: bucket=1, 3天后复习
再答对: bucket=2, 7天后复习
再答对: bucket=3, 21天后复习
答错: bucket=2, 7天后复习
再答错: bucket=1, 3天后复习
```

---

#### 4.3 动态重排剩余题目
**位置：** `app.py` 第3581-3656行

##### 步骤1：获取剩余未答题目
```python
remaining = items[idx + 1:]
```

##### 步骤2：使用Selector重新打分和选择
```python
if selector is not None:
    # 去重
    uniq_remaining = []
    seen_uids = set()
    for it in remaining:
        uid = get_item_uid(it)
        if uid not in seen_uids:
            seen_uids.add(uid)
            uniq_remaining.append(it)
    
    # 转换为ItemMeta
    candidate_metas = []
    for it in uniq_remaining:
        meta = ItemMeta(
            id=it.get("id", ""),
            difficulty_num=_parse_difficulty_num(it.get("difficulty")),
            knowledge_points=it.get("knowledge_points", [])
        )
        candidate_metas.append(meta)
    
    # 创建状态适配器（使用更新后的ability和review_schedule）
    class _S:
        def __init__(self, ability):
            self.ability = ability
            self.review_schedule = st.session_state.get("review_schedule", {})
            self.kp_mastery = {}
    
    state_adapter = _S(st.session_state.ability)
    
    # 重新选择（考虑更新后的能力值和复习计划）
    picks = selector.choose(
        candidate_metas,
        state_adapter,
        st.session_state.get("recent_correct_complex_ids", []),
        get_neighbors_fn,
        get_complex_difficulty_fn,
        k=len(candidate_metas)
    )
    
    # 重新构建题目列表
    resorted = [_find_item_by_meta(uniq_remaining, m) for m in picks]
else:
    # 降级：简单排序
    resorted = sort_items_for_adaptive(remaining, st.session_state.ability)

# 更新题目列表（已答 + 重排后的未答）
st.session_state.items = items[:idx + 1] + resorted
```

**说明：** 
- 每次答题后，系统会重新评估剩余题目的优先级
- 新的能力值和复习计划会影响后续题目的选择
- 到期题目会获得更高优先级

---

### 阶段五：学习循环

```
┌─────────────────────────────────────┐
│ 1. 显示当前题目                      │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 2. 用户作答                          │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 3. 点击Submit                        │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 4. 评估答案（is_correct）            │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 5. 更新能力值（±0.15）               │
│    记录答对的复杂题                  │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 6. 更新复习计划（Leitner）           │
│    bucket ±1, next_ts更新            │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 7. 重排剩余题目（Scorer+Selector）   │
│    考虑新能力值、复习计划            │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 8. item_idx++，显示下一题            │
└─────────────────────────────────────┘
              ↓
        回到步骤1
```

---

### 阶段六：轮次总结和历史保存

#### 6.1 保存轮次记录
**位置：** `app.py` 第1020-1067行
```python
def _save_round(
    ts_ms: int,
    duration_ms: int,
    total: int,
    answered: int,
    correct: int,
    filters: Dict,
    items_with_answers: List[Dict]
):
    # 计算准确率
    accuracy = correct / answered if answered > 0 else 0.0
    
    # 构建摘要记录
    round_summary = {
        "ts_ms": ts_ms,
        "duration_ms": duration_ms,
        "total": total,
        "answered": answered,
        "correct": correct,
        "accuracy": accuracy,
        "filters": filters,
        "detail_file": f"rounds/{ts_ms}_detail.json"
    }
    
    # 写入rounds.jsonl
    base = _history_dir()
    rounds_path = os.path.join(base, "rounds.jsonl")
    with open(rounds_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(round_summary, ensure_ascii=False) + "\n")
    
    # 写入详细记录
    detail_path = os.path.join(base, "rounds", f"{ts_ms}_detail.json")
    detail = {
        "ts_ms": ts_ms,
        "duration_ms": duration_ms,
        "filters": filters,
        "items": items_with_answers
    }
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
```

#### 6.2 显示轮次总结
**位置：** `app.py` 第1638-1933行
```python
def render_round_summary():
    # 1. 显示KPI卡片（总题数、已答、正确、准确率）
    # 2. 显示难度分布
    # 3. 显示错题回顾（可展开查看详情）
    # 4. 提供"Review Wrong Items"按钮重新学习
```

---

## 🧮 核心算法详解

### 算法1：Leitner间隔重复系统

**原理：** 基于遗忘曲线，动态调整复习间隔
```
桶0: 1天后复习（新题/多次错题）
桶1: 3天后复习（答对1次）
桶2: 7天后复习（答对2次）
桶3: 21天后复习（答对3次，掌握牢固）

答对：桶升级，间隔延长
答错：桶降级，间隔缩短
```

**数学模型：**
```
next_review_time = current_time + interval[bucket]
interval = [1天, 3天, 7天, 21天]
```

**代码位置：** `adaptive/scheduler.py`

---

### 算法2：难度-能力适配打分

**原理：** 最优学习区理论（Zone of Proximal Development）
```
难度过低 → 学习效率低
难度适中 → 学习效率最高（最优区）
难度过高 → 挫败感，放弃
```

**数学公式：**
```
difficulty_score = -|difficulty - ability|

例：
ability=2.5, difficulty=2 → score = -0.5
ability=2.5, difficulty=3 → score = -0.5
ability=2.5, difficulty=1 → score = -1.5  ❌ 太简单
ability=2.5, difficulty=5 → score = -2.5  ❌ 太难
```

**代码位置：** `adaptive/scorer.py` 第26-29行

---

### 算法3：相似题抑制（去重优化）

**原理：** 防止低效重复学习
```
场景：
- 用户答对了难度=4的题A
- 题B（难度=2）与A相似度很高（0.85）
- 继续出题B，浪费时间，学习效率低

解决：
- 检测题B与已答对的复杂题的相似度
- 如果相似且更简单，则降低题B的分数
```

**数学公式：**
```
penalty = suppress_lambda × similarity × difficulty_delta

suppress_lambda = 6.0
similarity = 0.85
difficulty_delta = difficulty_complex - difficulty_current
                 = 4 - 2 = 2

penalty = 6.0 × 0.85 × 2 = 10.2
```

**代码位置：** `adaptive/scorer.py` 第54-78行

---

### 算法4：错题增强（针对性训练）

**原理：** 刻意练习（Deliberate Practice）
```
场景：
- 用户答错了题A
- 题B与A在语义/知识点上相似
- 继续练习题B，强化薄弱环节

解决：
- 检测题B与最近错题的相似度
- 如果相似，则提高题B的分数
```

**数学公式：**
```
boost = boost_lambda × similarity

boost_lambda = 3.0
similarity = 0.80

boost = 3.0 × 0.80 = 2.4
```

**代码位置：** `adaptive/scorer.py` 第80-115行

---

### 算法5：Softmax概率采样

**原理：** 平衡探索（Exploration）与利用（Exploitation）
```
问题：
- 纯贪心选择：总是选分数最高的题（过度利用）
  → 陷入局部最优，缺乏多样性
  
- 纯随机选择：完全随机选题（过度探索）
  → 效率低，用户体验差

解决：
- Softmax将分数转换为概率分布
- 分数高的题被选中概率大，但不是100%
- 分数低的题仍有小概率被选中
```

**数学公式：**
```
P(item_i) = exp(score_i / temperature) / Σ exp(score_j / temperature)

temperature = 0.5
- 越小越确定性（倾向利用）
- 越大越随机（倾向探索）
```

**示例：**
```
scores = [5.0, 3.0, 1.0]
temperature = 0.5

exps = [exp(5.0/0.5), exp(3.0/0.5), exp(1.0/0.5)]
     = [exp(10), exp(6), exp(2)]
     = [22026, 403, 7.4]

probs = [22026/22436, 403/22436, 7.4/22436]
      = [0.982, 0.018, 0.0003]

题1被选中概率：98.2%
题2被选中概率：1.8%
题3被选中概率：0.03%
```

**代码位置：** `adaptive/selector.py` 第36-42行

---

### 算法6：向量相似度检索（Qdrant）

**原理：** 基于语义embedding的相似题检索
```
流程：
1. 将题目文本转换为1024维向量（embedding）
2. 存储到Qdrant向量数据库
3. 给定题目A，检索最相似的100道题
4. 返回(题目ID列表, 相似度列表)
```

**相似度计算：** 余弦相似度
```
similarity = cos(vector_A, vector_B) 
           = (A · B) / (|A| × |B|)

范围：[-1, 1]
- 1.0：完全相同
- 0.7~0.9：高度相似
- 0.0：无关
- -1.0：完全相反
```

**代码位置：** `app.py` 第3115-3139行

---

## 📊 数据流转图

### 会话状态变量
```python
st.session_state = {
    # 题目相关
    "items": List[Item],              # 当前题目列表
    "item_idx": int,                  # 当前题目索引
    
    # 统计数据
    "correct_count": int,             # 答对数量
    "answered_count": int,            # 已答数量
    "ability": float,                 # 能力值 [1.0, 5.0]
    
    # 答题记录
    "answered_items": Set[str],       # 已答题目ID集合
    "answers_by_item": Dict,          # {item_id: {choice, is_correct, ts_ms}}
    
    # 自适应数据
    "recent_correct_complex_ids": List[str],  # 最近答对的复杂题ID
    "review_schedule": Dict,          # {item_id: {bucket, next_ts_ms}}
    
    # 自适应模块
    "_selector": Selector,            # 选题器实例
    "_adaptive_params": AdaptiveParams, # 配置参数
    "_scheduler_intervals": Tuple,    # 复习间隔
    
    # UI状态
    "show_records": bool,             # 是否显示历史记录
    "show_summary": bool,             # 是否显示轮次总结
}
```

### 数据持久化
```
data/
├── history/
│   ├── rounds.jsonl              # 每行一轮摘要
│   │   {"ts_ms", "total", "answered", "correct", "accuracy", ...}
│   └── rounds/{ts}_detail.json   # 每轮详细记录
│       {"items": [{question, answer, user_choice, is_correct, ...}]}
└── adaptive_progress.json        # 当前进度（可保存/加载）
    {"items", "item_idx", "ability", "review_schedule", ...}
```

---

## 🔗 代码路径映射表

| 功能模块 | 文件路径 | 行号 | 说明 |
|---------|---------|------|------|
| **导入自适应模块** | `app.py` | 36-51 | 导入Scheduler, Scorer, Selector |
| **创建Selector** | `app.py` | 2023-2033 | _ensure_selector() |
| **加载题目** | `app.py` | 2051-2095 | load_filtered_items() |
| **智能选题** | `app.py` | 3097-3175 | selector.choose() |
| **打分算法** | `adaptive/scorer.py` | 18-52 | Scorer.score() |
| **相似题抑制** | `adaptive/scorer.py` | 54-78 | _similar_suppression() |
| **错题增强** | `adaptive/scorer.py` | 80-115 | _wrong_boost() |
| **Softmax采样** | `adaptive/selector.py` | 19-55 | Selector.choose() + _softmax() |
| **显示题目** | `app.py` | 2246-2600 | show_item_view() |
| **更新能力值** | `app.py` | 3546-3578 | ability ± 0.15 |
| **Leitner调度** | `app.py` | 3559-3575 | Scheduler.on_result() |
| **Leitner算法** | `adaptive/scheduler.py` | 16-30 | on_result() |
| **动态重排** | `app.py` | 3581-3656 | selector.choose() on remaining |
| **保存历史** | `app.py` | 1020-1067 | _save_round() |
| **轮次总结** | `app.py` | 1638-1933 | render_round_summary() |
| **学习记录** | `app.py` | 910-1377 | render_learning_records() |

---

## 🎯 关键设计亮点

1. **渐进式增强**
   - 如果Qdrant可用 → 使用向量检索
   - 如果不可用 → 降级为简单排序
   - 保证系统始终可用

2. **实时自适应**
   - 每次答题后立即更新能力值
   - 立即重排剩余题目
   - 不需要等到轮次结束

3. **探索-利用平衡**
   - 使用Softmax而非贪心选择
   - 避免陷入局部最优
   - 保持学习多样性

4. **认知科学支持**
   - Leitner间隔重复（遗忘曲线）
   - 最优学习区理论（难度适配）
   - 刻意练习（错题强化）

5. **高效去重**
   - 相似题抑制防止低效重复
   - 基于语义相似度而非简单去重
   - 保留有价值的相似题（不同角度）

---

## 📈 性能优化

1. **延迟加载**
   - 只加载max_items数量的题目
   - 避免一次性加载整个题库

2. **缓存机制**
   - Selector实例缓存在session_state
   - 向量邻居查询结果可缓存

3. **增量更新**
   - 只更新变化的状态
   - 避免全量重新计算

4. **异常降级**
   - 自适应模块异常时降级为简单排序
   - 保证核心功能不受影响

---

## 🔧 调优参数

如需调整学习曲线，可修改 `adaptive/config.py`:

```python
@dataclass(frozen=True)
class AdaptiveParams:
    # 相似度阈值（0.7=较严格，0.5=较宽松）
    sim_threshold: float = 0.70
    
    # 相似题抑制强度（越大抑制越强）
    suppress_lambda: float = 6.0
    
    # 错题增强强度（越大增强越强）
    boost_lambda: float = 3.0
    
    # 能力值步长（越大变化越快）
    ability_step_correct: float = +0.15
    ability_step_wrong: float = -0.15
    
    # Softmax温度（越小越确定性，越大越随机）
    softmax_temperature: float = 0.5
    
    # 复习间隔（天）
    review_intervals_days: tuple = (1, 3, 7, 21)
```

---

## 📚 参考文献

1. **Leitner System** - Sebastian Leitner (1972)
   - 间隔重复学习法

2. **Zone of Proximal Development** - Lev Vygotsky (1978)
   - 最优学习区理论

3. **Deliberate Practice** - Anders Ericsson (1993)
   - 刻意练习理论

4. **Softmax Exploration** - Sutton & Barto (2018)
   - 强化学习中的探索-利用平衡

5. **Semantic Similarity** - Qdrant
   - 基于向量embedding的语义相似度

---

## 🎓 总结

你的自适应学习系统是一个**研究级别的实现**，融合了：
- ✅ 认知科学（Leitner, ZPD）
- ✅ 机器学习（Softmax, Embedding）
- ✅ 教育心理学（Deliberate Practice）
- ✅ 系统工程（渐进式增强，异常降级）

这不是一个简单的题库系统，而是一个**真正的智能学习平台**！


