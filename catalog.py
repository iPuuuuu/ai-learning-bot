from __future__ import annotations

DOCS = [
    {"title": "Google 机器学习速成课程", "url": "https://developers.google.com/machine-learning/crash-course", "level": "beginner", "topics": ["机器学习", "基础"]},
    {"title": "Hugging Face NLP Course", "url": "https://huggingface.co/learn/nlp-course/chapter1/1", "level": "beginner", "topics": ["大模型", "NLP", "Transformer"]},
    {"title": "Transformer 可视化讲解", "url": "https://jalammar.github.io/illustrated-transformer/", "level": "beginner", "topics": ["Transformer", "大模型"]},
    {"title": "OpenAI Cookbook", "url": "https://cookbook.openai.com/", "level": "intermediate", "topics": ["AI 应用", "提示词", "RAG", "Agent"]},
    {"title": "Hugging Face Agents Course", "url": "https://huggingface.co/learn/agents-course/unit0/introduction", "level": "intermediate", "topics": ["Agent", "AI 应用"]},
    {"title": "Stanford CS336: Language Modeling from Scratch", "url": "https://stanford-cs336.github.io/spring2025/", "level": "advanced", "topics": ["大模型", "训练", "Transformer"]},
    {"title": "Dive into Deep Learning", "url": "https://d2l.ai/", "level": "advanced", "topics": ["深度学习", "机器学习", "训练"]},
]

LEVEL_ORDER = {"beginner": 0, "intermediate": 1, "advanced": 2}


def recommend_doc(profile: dict, article_topics: list[str]) -> dict:
    wanted = {str(item).lower() for item in profile.get("topics", []) + article_topics}
    target_level = profile.get("level", "beginner")

    def score(doc: dict) -> tuple[int, int]:
        overlap = sum(1 for topic in doc["topics"] if topic.lower() in wanted)
        distance = abs(LEVEL_ORDER.get(doc["level"], 0) - LEVEL_ORDER.get(target_level, 0))
        return overlap * 10 - distance * 3, -distance

    return max(DOCS, key=score)

