import tempfile
import unittest
from pathlib import Path

from articles import Article, infer_difficulty, infer_topics, score_article
from catalog import recommend_doc
from storage import Store


class CoreTests(unittest.TestCase):
    def test_personalized_scoring_prefers_topic_and_level(self):
        profile = {"level": "beginner", "topics": ["Agent"]}
        good = Article("A beginner guide to AI agents", "https://a", "test", "Agent introduction and explained")
        hard = Article("Gradient architecture research benchmark", "https://b", "test", "training paper architecture benchmark")
        self.assertGreater(score_article(good, profile, set()), score_article(hard, profile, set()))
        self.assertIn("Agent", infer_topics(good))
        self.assertEqual(infer_difficulty(good), "beginner")

    def test_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            profile = store.profile()
            profile["level"] = "advanced"
            store.save_profile(profile)
            self.assertEqual(store.profile()["level"], "advanced")
            article_id = store.save_article({
                "url": "https://example.com/a", "title": "A", "source": "example",
                "summary": "s", "content": "body", "published": "", "difficulty": "beginner", "topics": ["Agent"],
            }, "guide")
            self.assertEqual(store.current_article()["id"], article_id)

    def test_doc_matches_level(self):
        doc = recommend_doc({"level": "advanced", "topics": ["训练"]}, ["大模型"])
        self.assertEqual(doc["level"], "advanced")


if __name__ == "__main__":
    unittest.main()

