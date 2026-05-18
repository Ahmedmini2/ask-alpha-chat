import json
import boto3
from app.config import settings

_bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region)
EMBED_DIM = 1024


def embed_text(text: str) -> list[float]:
    """Returns a 1024-dim embedding for the given text via Bedrock Titan v2."""
    body = json.dumps({"inputText": text, "dimensions": EMBED_DIM, "normalize": True})
    response = _bedrock.invoke_model(
        modelId=settings.bedrock_embed_model_id,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    payload = json.loads(response["body"].read())
    return payload["embedding"]
