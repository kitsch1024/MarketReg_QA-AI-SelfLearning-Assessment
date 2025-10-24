"""贝叶斯能力值更新模块。

替代固定步长（±0.15），使用贝叶斯后验更新。
能力值表示为正态分布 N(μ, σ²)，根据答题结果动态更新。
"""

import math
from typing import Tuple


class BayesianAbilityTracker:
    """贝叶斯能力追踪器"""
    
    def __init__(self, initial_ability: float = 1.0, initial_variance: float = 1.0):
        """
        Args:
            initial_ability: 初始能力均值
            initial_variance: 初始不确定性（方差）
        """
        self.ability = initial_ability
        self.variance = initial_variance
    
    def update(self, is_correct: bool, difficulty: float) -> Tuple[float, float]:
        """根据答题结果更新能力值
        
        使用贝叶斯更新替代固定步长：
        - 旧算法：ability ± 0.15（固定）
        - 新算法：根据当前不确定性动态调整
        
        Args:
            is_correct: 是否答对
            difficulty: 题目难度 [1-5]
        
        Returns:
            (new_ability, new_variance)
        """
        # 答对概率模型：logistic函数
        # P(correct | ability, difficulty) = 1 / (1 + exp(-(ability - difficulty)))
        
        # 计算预测概率
        predicted_prob = self._sigmoid(self.ability - difficulty)
        
        # 观测值（0或1）
        observation = 1.0 if is_correct else 0.0
        
        # 预测误差
        error = observation - predicted_prob
        
        # Fisher信息量（曲率）
        fisher_info = predicted_prob * (1 - predicted_prob)
        
        # 贝叶斯更新（简化版Newton-Raphson）
        # 步长 = 方差 × 梯度
        learning_rate = self.variance * fisher_info
        
        # 限制学习率范围
        learning_rate = max(0.05, min(learning_rate, 0.5))
        
        # 更新能力值
        self.ability += learning_rate * error
        
        # 限制能力值范围 [1.0, 5.0]
        self.ability = max(1.0, min(5.0, self.ability))
        
        # 更新不确定性（方差收缩）
        # 随着答题增多，不确定性降低
        self.variance = self.variance * (1 - fisher_info * self.variance * 0.1)
        
        # 限制方差范围 [0.1, 2.0]
        self.variance = max(0.1, min(2.0, self.variance))
        
        return self.ability, self.variance
    
    @staticmethod
    def _sigmoid(x: float) -> float:
        """Sigmoid函数（数值稳定版本）"""
        if x >= 0:
            z = math.exp(-x)
            return 1 / (1 + z)
        else:
            z = math.exp(x)
            return z / (1 + z)
    
    def get_confidence_interval(self, confidence: float = 0.95) -> Tuple[float, float]:
        """计算置信区间
        
        Args:
            confidence: 置信水平（默认95%）
        
        Returns:
            (lower_bound, upper_bound)
        """
        # 95%置信区间：μ ± 1.96σ
        z_score = 1.96 if confidence == 0.95 else 2.58
        margin = z_score * math.sqrt(self.variance)
        
        lower = max(1.0, self.ability - margin)
        upper = min(5.0, self.ability + margin)
        
        return lower, upper


def update_ability_bayesian(
    current_ability: float,
    current_variance: float,
    is_correct: bool,
    difficulty: float
) -> Tuple[float, float]:
    """函数式接口（用于直接调用）
    
    Args:
        current_ability: 当前能力值
        current_variance: 当前方差
        is_correct: 是否答对
        difficulty: 题目难度
    
    Returns:
        (new_ability, new_variance)
    """
    tracker = BayesianAbilityTracker(current_ability, current_variance)
    return tracker.update(is_correct, difficulty)

