"""自适应选择流程所用的数据模型。

- ItemMeta：选择器使用的题目元数据（标准化）
- AnswerRecord：一次提交的简记录（正误 + 时间戳）
- ReviewEntry：简化的 Leitner 复习条目（桶 + 下次时间）
- SessionState：打分/选择器消费的会话数据子集
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ItemMeta:
    id: str
    field: Optional[str]
    type: Optional[str]
    difficulty_str: Optional[str]
    difficulty_num: int
    standard: Optional[str]
    doc: Optional[str]
    knowledge_points: List[str] = field(default_factory=list)


@dataclass
class AnswerRecord:
    is_correct: Optional[bool]
    ts_ms: int


@dataclass
class ReviewEntry:
    """SM-2算法的复习条目（替代Leitner分桶）"""
    easiness_factor: float  # 易度因子 EF，初始2.5
    interval_days: float    # 当前间隔（天）
    repetitions: int        # 连续答对次数
    next_ts_ms: int         # 下次复习时间戳
    
    # 兼容旧接口
    @property
    def bucket(self) -> int:
        """模拟桶号（用于向后兼容）"""
        if self.interval_days <= 1:
            return 0
        elif self.interval_days <= 3:
            return 1
        elif self.interval_days <= 7:
            return 2
        else:
            return 3


@dataclass
class SessionState:
    ability: float
    answers_by_item: Dict[str, AnswerRecord]
    answered_items: Set[str]
    review_schedule: Dict[str, ReviewEntry]
    kp_mastery: Dict[str, int]
    
    # 贝叶斯能力追踪
    ability_variance: float = 1.0  # 能力值的不确定性
    
    # Q-Learning状态
    q_values: Dict[str, float] = field(default_factory=dict)  # item_id -> Q值
    
    # UCB统计
    item_selection_counts: Dict[str, int] = field(default_factory=dict)  # 选择次数
    total_selections: int = 0  # 总选择次数


