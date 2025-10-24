"""SM-2算法（SuperMemo 2）间隔重复调度器。

替代Leitner分桶系统，使用连续的易度因子(EF)和间隔计算。
更精细的个性化间隔调整。
"""

import time
from typing import Tuple
from .models import ReviewEntry


class Scheduler:
    """SM-2算法实现（保持接口兼容）"""
    
    def __init__(self, intervals_days: Tuple[int, ...]):
        # 保留参数以兼容旧接口，但不使用
        self.intervals_days = intervals_days
        self.min_ef = 1.3  # 易度因子最小值
    
    def on_result(self, entry: ReviewEntry | None, is_correct: bool, now_ms: int | None = None) -> ReviewEntry:
        """SM-2算法：根据答题质量更新易度因子和间隔
        
        接口保持100%兼容，内部逻辑完全改变：
        - Leitner: 离散桶 → SM-2: 连续EF因子
        - 固定间隔 → 动态计算间隔
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        
        # 获取当前状态（如果是新题，初始化）
        if entry is None:
            ef = 2.5  # 初始易度因子
            interval = 1.0  # 首次间隔1天
            reps = 0
        else:
            ef = entry.easiness_factor
            interval = entry.interval_days
            reps = entry.repetitions
        
        # SM-2核心算法
        if is_correct:
            # 答对：增加重复次数
            reps += 1
            
            # 计算答题质量（简化版，实际应该让用户评分0-5）
            # 这里假设答对质量为4（良好）
            quality = 4
            
            # 更新易度因子
            ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            ef = max(self.min_ef, ef)  # 限制最小值
            
            # 计算新间隔
            if reps == 1:
                interval = 1.0
            elif reps == 2:
                interval = 6.0
            else:
                interval = interval * ef
        else:
            # 答错：重新开始，但保留部分EF
            reps = 0
            interval = 1.0
            
            # 答题质量为2（困难）
            quality = 2
            
            # EF下降更多
            ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            ef = max(self.min_ef, ef)
        
        # 限制最大间隔（避免过长）
        interval = min(interval, 180.0)  # 最多180天
        
        # 计算下次复习时间
        next_ts = now_ms + int(interval * 86400000)  # 转换为毫秒
        
        return ReviewEntry(
            easiness_factor=ef,
            interval_days=interval,
            repetitions=reps,
            next_ts_ms=next_ts
        )


