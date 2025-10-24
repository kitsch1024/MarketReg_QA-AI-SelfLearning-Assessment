"""自适应学习配置。

集中管理可调参数：
- QdrantConfig：向量检索的连接与集合配置
- AdaptiveParams：相似题抑制/错题增强、能力动态、近邻规模、初始能力等
- FilesConfig：可选的邻居/状态文件默认路径

仅包含常量/数据类，应用可直接导入使用。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QdrantConfig:
    host: str = "192.168.23.246"
    port: int = 6333
    prefer_grpc: bool = True
    collection: str = "MarketReg_QA"
    vector_size: int = 1024
    timeout: int = 30


@dataclass(frozen=True)
class AdaptiveParams:
    sim_threshold: float = 0.70
    suppress_lambda: float = 6.0
    complex_difficulty_min: int = 3
    topk_neighbors: int = 100
    boost_lambda: float = 3.0  # 对做错题目的相似题增加曝光的加权系数

    ability_init: float = 1.0
    ability_step_correct: float = +0.15
    ability_step_wrong: float = -0.15
    softmax_temperature: float = 0.5

    review_intervals_days: tuple[int, ...] = (1, 3, 7, 21)


@dataclass(frozen=True)
class FilesConfig:
    neighbors_path: str = "data/neighbors.jsonl"
    state_path: str = "data/adaptive_state.json"


