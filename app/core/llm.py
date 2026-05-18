import boto3
from app.config import settings

class LLMClient:
    def __init__(self):
        self.client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        self.model_id = settings.bedrock_model_id

    def chat(self, system: str, user: str, max_tokens: int = 1024) -> str:
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3},
        )
        return response["output"]["message"]["content"][0]["text"]

llm = LLMClient()
