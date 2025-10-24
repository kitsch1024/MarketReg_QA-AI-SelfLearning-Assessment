"""UCB选择器：Upper Confidence Bound。

替代Softmax采样，使用UCB（置信上界）策略。
理论保证最优的探索-利用平衡。
"""

import math
import random
from typing import Callable, Iterable, List, Optional, Tuple
from .models import ItemMeta, SessionState
from .scorer import Scorer


class Selector:
    """UCB选择器（接口兼容，算法完全不同）"""
    
    def __init__(self, scorer: Scorer, temp: float):
        self.scorer = scorer
        # temp参数保留以兼容，但UCB不使用温度参数
        self.ucb_c = math.sqrt(2)  # UCB探索系数
    
    def choose(
        self,
        candidates: Iterable[ItemMeta],
        state: SessionState,
        recent_correct_complex_ids: List[str],
        get_neighbors_fn: Callable[[str], Optional[Tuple[List[str], List[float]]]],
        get_complex_difficulty_fn: Callable[[str], int],
        k: int = 20,
    ) -> List[ItemMeta]:
        """UCB选择（算法完全改变，接口不变）
        
        旧算法：Softmax概率采样
        新算法：UCB置信上界选择
        
        UCB(item) = Q(item) + c × √(ln(N) / n(item))
        
        优势：理论保证，自动平衡探索与利用
        """
        # 1. 为每个候选题计算UCB分数
        ucb_scored: List[Tuple[ItemMeta, float, float]] = []
        
        for it in candidates:
            # 基础Q分数（来自Scorer）
            q_score = self.scorer.score(
                it, state, recent_correct_complex_ids, 
                get_neighbors_fn, get_complex_difficulty_fn
            )
            
            # UCB探索项
            selection_count = state.item_selection_counts.get(it.id, 0)
            total_selections = state.total_selections
            
            if selection_count == 0:
                # 未选过的题目：给予无限大的探索优先级
                ucb_bonus = float('inf')
            elif total_selections > 0:
                # UCB公式：c × √(ln(N) / n)
                ucb_bonus = self.ucb_c * math.sqrt(
                    math.log(total_selections) / selection_count
                )
            else:
                ucb_bonus = 0.0
            
            # UCB总分
            ucb_score = q_score + ucb_bonus
            
            ucb_scored.append((it, ucb_score, q_score))
        
        # 2. UCB选择策略
        if k >= len(ucb_scored):
            # 🆕 先检查是否所有分数都相同（特别是都是无穷大的情况）
            # 如果是，则完全随机打乱；否则按分数排序
            all_scores = [score for _, score, _ in ucb_scored]
            if len(set(all_scores)) == 1 or all(s == float('inf') for s in all_scores):
                # 所有分数相同（或都是无穷大），完全随机打乱
                random.shuffle(ucb_scored)
            else:
                # 分数有差异，按UCB分数排序
                ucb_scored.sort(key=lambda x: x[1], reverse=True)
            return [it for it, _, _ in ucb_scored]
        
        # 3. Top-k UCB选择
        # 🆕 添加小的随机扰动打破平局（特别是初始状态）
        ucb_scored_with_noise = []
        for it, ucb_score, q_score in ucb_scored:
            # 如果UCB分数是无穷大，添加随机noise
            if ucb_score == float('inf'):
                noise = random.random() * 0.1  # 小的随机扰动
                ucb_scored_with_noise.append((it, ucb_score, q_score, noise))
            else:
                ucb_scored_with_noise.append((it, ucb_score, q_score, 0.0))
        
        # 先按UCB分数排序，再按noise排序（处理无穷大的情况）
        ucb_scored_with_noise.sort(key=lambda x: (x[1], x[3]), reverse=True)
        top_k = [(it, ucb_score, q_score) for it, ucb_score, q_score, _ in ucb_scored_with_noise[:k]]
        
        # 4. 在top-k内部随机打乱（增加多样性）
        random.shuffle(top_k)
        
        return [it for it, _, _ in top_k]
    
    def _softmax(self, xs: List[float], temp: float) -> List[float]:
        """保留以兼容（但UCB不使用）"""
        if temp <= 0:
            temp = 0.1
        m = max(xs) if xs else 0.0
        exps = [math.exp((x - m) / temp) for x in xs]
        Z = sum(exps) or 1.0
        return [e / Z for e in exps]
    
    def _multinomial(self, scored, probs, k) -> List[ItemMeta]:
        """保留以兼容（但UCB不使用）"""
        items = [it for it, _ in scored]
        res: List[ItemMeta] = []
        for _ in range(min(k, len(items))):
            r = random.random()
            acc = 0.0
            for it, p in zip(items, probs):
                acc += p
                if r <= acc:
                    res.append(it)
                    break
        return res


