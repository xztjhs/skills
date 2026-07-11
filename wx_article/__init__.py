"""
wx_article – 微信公众号文章提取器（OpenClaw Skill）

用法:
    from wx_article.scripts.extract import WxArticleExtractor, Config
    extractor = WxArticleExtractor()
    path = extractor.extract("https://mp.weixin.qq.com/s/xxxxx")
"""

from wx_article.scripts.extract import WxArticleExtractor, Config, Article, Stats

__all__ = ["WxArticleExtractor", "Config", "Article", "Stats"]
