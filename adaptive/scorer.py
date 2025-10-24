"""Q-Learning打分系统。

替代线性打分，使用强化学习的Q值估计。
从历史rounds.jsonl数据初始化，在线学习更新。

Q(state, item) = 期望累积回报
"""

import time
from typing import Callable, Dict, List, Optional, Tuple
from .models import ItemMeta, SessionState


class Scorer:
    """Q-Learning打分器（接口兼容，算法完全不同）"""
    
    def __init__(self, params):
        self.p = params
        
        # Q-Learning超参数
        self.alpha = 0.1  # 学习率
        self.gamma = 0.9  # 折扣因子
        self.epsilon = 0.1  # 探索率
        
        # Q值表：默认值为0
        # 会从rounds.jsonl初始化
        self.q_values: Dict[str, float] = {}
    
    def initialize_from_history(self, history_data: List[Dict]) -> None:
        """从历史记录初始化Q值
        
        Args:
            history_data: 最近15条rounds记录
        """
        # 统计每道题的表现
        item_stats: Dict[str, List[bool]] = {}
        
        for round_data in history_data:
            items = round_data.get("items", [])
            for item_data in items:
                item_id = item_data.get("id", "")
                is_correct = item_data.get("is_correct")
                
                if item_id and is_correct is not None:
                    if item_id not in item_stats:
                        item_stats[item_id] = []
                    item_stats[item_id].append(is_correct)
        
        # 计算初始Q值：基于历史表现
        for item_id, results in item_stats.items():
            correct_count = sum(results)
            total_count = len(results)
            
            # Q值 = 答对率 × 5.0 - 2.0（归一化到合理范围）
            accuracy = correct_count / total_count
            self.q_values[item_id] = accuracy * 5.0 - 2.0
    
    def score(
        self,
        item: ItemMeta,
        state: SessionState,
        recent_correct_complex_ids: List[str],
        get_neighbors_fn: Callable[[str], Optional[Tuple[List[str], List[float]]]],
        get_complex_difficulty_fn: Callable[[str], int],
    ) -> float:
        """Q-Learning打分（算法完全改变，接口不变）
        
        旧算法：Score = 难度 + 复习 + 覆盖 - 抑制 + 增强
        新算法：Q(state, item) = Q值 + 探索奖励 + 复习加成
        """
        # 1. 基础Q值
        base_q = self.q_values.get(item.id, 0.0)
        
        # 2. 难度适配奖励（保留核心逻辑）
        difficulty_reward = -abs(item.difficulty_num - state.ability) * 0.5
        
        # 3. 复习优先加成（继承原有逻辑）
        review_bonus = 0.0
        entry = state.review_schedule.get(item.id)
        if entry:
            now = int(time.time() * 1000)
            # 兼容性处理：entry可能是dict（旧格式）或ReviewEntry对象（新格式）
            if isinstance(entry, dict):
                next_ts = entry.get("next_ts_ms", 0)
            else:
                next_ts = entry.next_ts_ms
            
            if now >= next_ts:
                overdue_days = (now - next_ts) / 86400000.0
                review_bonus = 3.0 + min(2.0, overdue_days)
        
        # 4. 探索奖励（UCB思想）
        selection_count = state.item_selection_counts.get(item.id, 0)
        if state.total_selections > 0 and selection_count > 0:
            import math
            exploration_bonus = math.sqrt(2 * math.log(state.total_selections) / selection_count)
        else:
            # 新题目，给予探索奖励
            exploration_bonus = 2.0
        
        # 5. 知识点价值（保留）
        knowledge_value = 0.0
        if item.knowledge_points:
            uncovered = [
                kp for kp in item.knowledge_points 
                if state.kp_mastery.get(kp, 0) < item.difficulty_num
            ]
            if uncovered:
                knowledge_value = len(uncovered) * 0.3
        
        # 6. 相似题抑制（保留Qdrant逻辑）
        similarity_penalty = self._similar_suppression(
            item, recent_correct_complex_ids, get_neighbors_fn, get_complex_difficulty_fn
        )
        
        # 7. 错题增强（保留Qdrant逻辑）
        wrong_boost = self._wrong_boost(item, state, get_neighbors_fn)
        
        # Q-Learning总分
        total_score = (
            base_q + 
            difficulty_reward + 
            review_bonus + 
            exploration_bonus * 0.5 + 
            knowledge_value - 
            similarity_penalty * 0.3 + 
            wrong_boost * 0.5
        )
        
        return total_score
    
    def update_q_value(
        self, 
        item_id: str, 
        reward: float, 
        next_max_q: float = 0.0
    ) -> None:
        """Q值更新（在线学习）
        
        Q(s,a) ← Q(s,a) + α[r + γ·max Q(s',a') - Q(s,a)]
        
        Args:
            item_id: 题目ID
            reward: 即时奖励（答对+1, 答错-0.5）
            next_max_q: 下一状态的最大Q值
        """
        current_q = self.q_values.get(item_id, 0.0)
        
        # Bellman更新
        td_target = reward + self.gamma * next_max_q
        td_error = td_target - current_q
        new_q = current_q + self.alpha * td_error
        
        self.q_values[item_id] = new_q

    def _similar_suppression(
        self,
        item: ItemMeta,
        complex_ids: List[str],
        get_neighbors_fn: Callable[[str], Optional[Tuple[List[str], List[float]]]],
        get_complex_difficulty_fn: Callable[[str], int],
    ) -> float:
        penalty = 0.0
        for cid in complex_ids:
            nn = get_neighbors_fn(cid)
            if not nn:
                continue
            nn_ids, nn_sims = nn
            try:
                idx = nn_ids.index(item.id)
            except ValueError:
                continue
            sim = nn_sims[idx]
            if sim < self.p.sim_threshold:
                continue
            d_complex = max(self.p.complex_difficulty_min, get_complex_difficulty_fn(cid))
            if item.difficulty_num <= d_complex - 1:
                diff_delta = d_complex - item.difficulty_num
                penalty += self.p.suppress_lambda * sim * (diff_delta)
        return penalty

    def _wrong_boost(
        self,
        item: ItemMeta,
        state: SessionState,
        get_neighbors_fn: Callable[[str], Optional[Tuple[List[str], List[float]]]],
    ) -> float:
        # 思路：对最近做错的题（不要求复杂题），其相似题给予适度加分，鼓励温习
        # 需要上层在 state.answers_by_item 中能区分最近错题；这里简化：取最近 N 个错题（按 ts 排序）
        wrong_ids: List[str] = []
        try:
            # 将 AnswerRecord 增添 ts_ms 后可排序；此处做安全访问
            wrong_pairs = [
                (qid, rec.ts_ms)
                for qid, rec in state.answers_by_item.items()
                if rec.is_correct is False
            ]
            wrong_pairs.sort(key=lambda x: x[1], reverse=True)
            wrong_ids = [qid for qid, _ in wrong_pairs[:10]]
        except Exception:
            return 0.0

        boost = 0.0
        for wid in wrong_ids:
            nn = get_neighbors_fn(wid)
            if not nn:
                continue
            nn_ids, nn_sims = nn
            try:
                idx = nn_ids.index(item.id)
            except ValueError:
                continue
            sim = nn_sims[idx]
            if sim <= 0:
                continue
            boost += self.p.boost_lambda * sim
        return boost


