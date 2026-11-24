from abc import ABC, abstractmethod


class BaseEnricher(ABC):
    @abstractmethod
    def enrich(self, kind: str, values: dict, source_url: str) -> dict:
        """Return fields supported by the source evidence."""


class LLMEnricher(BaseEnricher):
    def enrich(self, kind, values, source_url):
        raise RuntimeError("一期未配置模型服务")
